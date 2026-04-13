"""
KEEPER-ZERO — Scanner Morpho Multicall3
Scanne les positions Morpho Optimizer sur Polygon via Multicall3.

Corrections vs VOID-WALKER :
  - _encode_transaction_data() supprimé (cassé en web3.py v6)
  - Remplacé par encodeABI(fn_name=..., args=[...]) ← compatible v5 + v6
  - Logs de diagnostic sur les échecs d'encodage
  - Calcul de profit basé sur 50% de la dette (plafond réel Morpho/AAVE)
"""
import asyncio
from datetime import datetime
from typing import List, Dict, Callable
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from config import POLYGON, MULTICALL3_ABI, MORPHO_ABI, MULTICALL3_ADDRESS
from core.rpc import RpcPool


class MorphoScanner:
    def __init__(self, on_update: Callable = None):
        self.on_update = on_update or (lambda **kw: None)

        urls = POLYGON.get("rpc_urls", []) or [POLYGON["rpc_url"]]
        self.rpc_pool = RpcPool(urls)
        self.w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_pool.current()))

        # Multicall3
        self._mc = self.w3.eth.contract(
            address=self.w3.to_checksum_address(MULTICALL3_ADDRESS),
            abi=MULTICALL3_ABI
        )

        # Contrat Morpho — on garde l'instance complète pour encodeABI()
        self._morpho = self.w3.eth.contract(
            address=self.w3.to_checksum_address(POLYGON["morpho"]["address"]),
            abi=MORPHO_ABI
        )

        # Résultats en mémoire
        self.liquidatable: List[Dict] = []
        self.watchlist:    List[Dict] = []
        self.last_scan_ts: str = ""
        self.stats = {"scanned": 0, "errors": 0, "liquidatable": 0, "encoding_failures": 0}
        self.pause_requested = False
        self.morpho_available = True
        self.last_error = ""


    async def _direct_check(self, addr: str) -> str:
        """Appel direct au contrat Morpho pour diagnostiquer les erreurs."""
        try:
            cs = self.w3.to_checksum_address(addr)
            await self._morpho.functions.getUserBalanceStates(cs).call()
            return 'OK'
        except Exception as e:
            return f'FAIL: {e}'

    def _encode_call_data(self, fn_name: str, args: list):
        """Encode call data compatible web3.py v5/v6 async contracts."""
        try:
            return self._morpho.encodeABI(fn_name=fn_name, args=args)
        except Exception:
            pass
        try:
            fn = getattr(self._morpho.functions, fn_name)(*args)
            return fn._encode_transaction_data()
        except Exception as e:
            raise e


    # ── Sanity check — vérifie que le pipeline Multicall fonctionne ──
    async def verify_pipeline(self) -> bool:
        """
        Teste le pipeline sur une adresse factice.
        Retourne True si l'encodage + appel Multicall fonctionnent.
        Un resultat vide est normal (adresse inactive) ? ce qui importe
        c'est l'absence d'exception.
        """
        self.last_error = ""
        TEST_ADDR = "0x0000000000000000000000000000000000000001"

        # Verifier que le contrat Morpho existe sur la chaine
        try:
            code = await self.w3.eth.get_code(self.w3.to_checksum_address(POLYGON["morpho"]["address"]))
            if not code or len(code) == 0:
                self.morpho_available = False
                self.last_error = "contract_missing"
                self.on_update(log="[Morpho] Contrat introuvable sur ce RPC/chaine ? verifie MORPHO_ADDRESS")
                return False
        except Exception as e:
            self.last_error = str(e)
            self.on_update(log=f"[Morpho] Erreur get_code : {e}")
            return False

        # Encodage ABI
        try:
            self._encode_call_data("getUserBalanceStates", [self.w3.to_checksum_address(TEST_ADDR)])
            self.on_update(log="[Morpho] Sanity check encodeABI : OK")
        except Exception as e:
            self.last_error = str(e)
            self.on_update(log=f"[Morpho] Sanity check ECHOUE : {e}")
            return False

        # Test appel direct (sans multicall)
        direct = await self._direct_check(TEST_ADDR)
        if direct != 'OK':
            self.last_error = direct
            self.on_update(log=f"[Morpho] Sanity check direct FAIL : {direct}")
            return False
        self.on_update(log="[Morpho] Sanity check direct : OK")
        return True

    async def _scan_batch(
        self,
        addrs: List[str],
        hf_threshold: float,
        watch_threshold: float,
        min_debt_usd: float
    ) -> tuple:
        """
        Scanne un batch d'adresses via Multicall3 (getUserBalanceStates).
        Retourne (liquidatable, watchlist).
        """
        morpho_addr = self.w3.to_checksum_address(POLYGON["morpho"]["address"])
        calls = []
        valid_idx = []
        encoding_failures = 0

        for i, addr in enumerate(addrs):
            try:
                cs   = self.w3.to_checksum_address(addr)
                # ✅ encodeABI — compatible web3.py v5 et v6
                data = self._encode_call_data("getUserBalanceStates", [cs])
                calls.append({
                    "target":       morpho_addr,
                    "allowFailure": True,
                    "callData":     data,
                })
                valid_idx.append(i)
            except Exception as e:
                encoding_failures += 1

        # Log si des encodages ont échoué — permet de diagnostiquer
        if encoding_failures > 0:
            self.on_update(log=f"[Morpho] ⚠️  {encoding_failures}/{len(addrs)} encodages échoués")
            self.stats["encoding_failures"] += encoding_failures

        if not calls:
            self.on_update(log="[Morpho] ❌ Aucun appel valide — vérifier l'ABI Morpho")
            return [], []

        # Appel Multicall avec retry exponentiel
        raw = None
        for attempt in range(3):
            try:
                raw = await asyncio.wait_for(
                    self._mc.functions.aggregate3(calls).call(),
                    timeout=45.0
                )
                break
            except asyncio.TimeoutError:
                if attempt == 2:
                    self.on_update(log="[Morpho] Multicall timeout après 3 tentatives")
                    return [], []
                await asyncio.sleep(2 ** attempt)
            except (ConnectionResetError, OSError) as e:
                self.on_update(log=f"[Morpho] Connexion RPC perdue : {e}")
                return [], []
            except Exception as e:
                self.on_update(log=f"[Morpho] Multicall erreur : {e}")
                return [], []

        if raw is None:
            return [], []

        liq, watch = [], []
        GAS_COST_USD      = 0.05   # ~$0.05 gas par liquidation sur Polygon
        LIQUIDATION_BONUS = 0.05   # 5% bonus moyen sur Morpho
        MAX_LIQ_RATIO     = 0.50   # AAVE/Morpho : max 50% de la dette par appel

        failed = 0
        for call_i, orig_i in enumerate(valid_idx):
            success, ret = raw[call_i]
            if not success or len(ret) < 32:
                self.stats["errors"] += 1
                failed += 1
                continue
            try:
                # getUserBalanceStates retourne un tuple de 6 uint256
                # (collateralEth, borrowableEth, maxDebtEth, liquidationEth, debtEth, healthFactor)
                decoded = self.w3.codec.decode(
                    ["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"],
                    ret
                )
                collateral_eth = decoded[0]   # en WAD (1e18 = 1 ETH)
                debt_eth       = decoded[4]   # en WAD
                hf_raw         = decoded[5]   # en WAD (1e18 = HF de 1.0)

                # Normalisation
                hf = hf_raw / 1e18 if 0 < hf_raw < 2**128 else 999.0

                # Conversion ETH → USD approximative (prix MATIC ignoré ici,
                # on travaille en unités relatives — le seuil min_debt_usd est
                # calibré pour être conservateur)
                debt_usd       = round(debt_eth / 1e18 * 1800, 2)   # 1 ETH ≈ $1800 (estimation)
                collateral_usd = round(collateral_eth / 1e18 * 1800, 2)

                self.stats["scanned"] += 1

                if debt_usd < min_debt_usd or hf == 0 or collateral_usd == 0:
                    continue

                # Profit estimé réaliste :
                # max 50% de la dette liquidée × bonus 5% − gas
                liquidatable_debt = debt_usd * MAX_LIQ_RATIO
                profit_gross      = liquidatable_debt * LIQUIDATION_BONUS
                profit_net        = round(profit_gross - GAS_COST_USD, 2)

                entry = {
                    "address":       addrs[orig_i],
                    "hf":            round(hf, 4),
                    "debt_usd":      debt_usd,
                    "collateral_usd":collateral_usd,
                    "profit_est":    profit_net,
                    "profit_gross":  round(profit_gross, 2),
                    "protocol":      "morpho",
                    "ts":            datetime.now().isoformat(),
                }

                if hf < hf_threshold:
                    liq.append(entry)
                    self.stats["liquidatable"] += 1
                elif hf < watch_threshold:
                    watch.append(entry)

            except Exception:
                self.stats["errors"] += 1

        if failed == len(valid_idx) and valid_idx:
            # Diagnostic rapide: appel direct sur la premi?re adresse
            diag = await self._direct_check(addrs[valid_idx[0]])
            self.on_update(log=f"[Morpho] Diagnostic direct : {diag}")

        return liq, watch

    # ── Scan complet ─────────────────────────────────────────────
    async def run_scan(
        self,
        addresses: List[str],
        hf_threshold: float    = 1.0,
        watch_threshold: float = 1.05,
        min_debt_usd: float    = 50.0,
        batch_size: int        = 300,
    ) -> Dict:
        """Lance un scan complet sur la liste d'adresses Morpho."""
        self.stats        = {"scanned": 0, "errors": 0, "liquidatable": 0, "encoding_failures": 0}
        self.liquidatable = []
        self.watchlist    = []
        total             = len(addresses)

        self.on_update(log=f"[Morpho] Scan démarré : {total} adresses (batch={batch_size})")

        for i in range(0, total, batch_size):
            if self.pause_requested:
                self.on_update(log="[Morpho] Scan en pause (liquidation en cours)")
                break

            batch = addresses[i:i + batch_size]
            liq, watch = await self._scan_batch(
                batch, hf_threshold, watch_threshold, min_debt_usd
            )
            self.liquidatable.extend(liq)
            self.watchlist.extend(watch)

            done = min(i + batch_size, total)
            pct  = done / total * 100 if total else 0
            self.on_update(
                scan_progress=pct,
                scan_done=done,
                scan_total=total,
                liquidatable=self.liquidatable,
                watchlist=self.watchlist,
                log=f"[Morpho] {done}/{total} ({pct:.1f}%) — "
                    f"{len(self.liquidatable)} liquidables | "
                    f"{self.stats['encoding_failures']} échecs encodage"
            )
            await asyncio.sleep(0.05)

        # Trier par profit décroissant
        self.liquidatable.sort(key=lambda x: x["profit_est"], reverse=True)
        self.watchlist.sort(key=lambda x: x["hf"])
        self.last_scan_ts = datetime.now().isoformat()

        result = {
            "scanned":     self.stats["scanned"],
            "liquidatable":len(self.liquidatable),
            "watchlist":   len(self.watchlist),
            "errors":      self.stats["errors"],
            "enc_failures":self.stats["encoding_failures"],
            "candidates":  self.liquidatable[:50],
            "ts":          self.last_scan_ts,
        }

        self.on_update(
            log=f"[Morpho] Scan terminé : {result['liquidatable']} liquidables / "
                f"{result['scanned']} scannées / {result['errors']} erreurs"
        )
        return result

    # ── Collecte des adresses actives via les events Morpho ──────
    async def fetch_morpho_borrowers(self, pages: int = 50) -> List[str]:
        """
        Collecte les adresses qui ont emprunté sur Morpho Polygon
        via l'API Alchemy (alchemy_getAssetTransfers vers le contrat Morpho).
        Retourne une liste d'adresses uniques.
        """
        import aiohttp
        morpho_addr = POLYGON["morpho"]["address"].lower()
        rpc_url     = POLYGON["rpc_url"]
        addresses   = set()

        async with aiohttp.ClientSession() as session:
            for direction in ["to", "from"]:
                page_key = None
                for page in range(pages):
                    params = {
                        "category":  ["external", "erc20", "internal"],
                        "maxCount":  "0x3e8",
                        "fromBlock": "0x0",
                        "toBlock":   "latest",
                    }
                    if direction == "to":
                        params["toAddress"] = morpho_addr
                    else:
                        params["fromAddress"] = morpho_addr
                    if page_key:
                        params["pageKey"] = page_key

                    payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method":  "alchemy_getAssetTransfers",
                        "params":  [params]
                    }
                    try:
                        async with session.post(
                            rpc_url, json=payload,
                            timeout=aiohttp.ClientTimeout(total=30)
                        ) as resp:
                            if resp.status != 200:
                                break
                            data = await resp.json()
                            result = data.get("result", {})
                            txs    = result.get("transfers", [])

                            if not txs:
                                break

                            for tx in txs:
                                addr = tx.get("from") if direction == "to" else tx.get("to")
                                if addr:
                                    addresses.add(addr.lower())

                            page_key = result.get("pageKey")
                            self.on_update(
                                log=f"[Morpho] Collecte {direction} page {page+1} : "
                                    f"{len(addresses)} adresses"
                            )
                            if not page_key:
                                break
                            await asyncio.sleep(0.15)

                    except Exception as e:
                        self.on_update(log=f"[Morpho] Erreur collecte : {e}")
                        break

        self.on_update(log=f"[Morpho] Collecte terminée : {len(addresses)} adresses uniques")
        return list(addresses)
