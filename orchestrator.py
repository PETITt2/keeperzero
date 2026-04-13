"""
KEEPER-ZERO — Orchestrateur principal
Boucle : Collecte adresses → Scan Morpho → Execute liquidation → Harvest Beefy

Architecture simplifiée vs VOID-WALKER :
  - Pas de GUI, pas de Tkinter
  - Logs structurés dans SQLite pour analyse des performances
  - Deux workers indépendants : morpho_loop + beefy_loop
  - Retrait automatique des profits après chaque succès
"""
import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from eth_account import Account

from config import POLYGON, SETTINGS, ERC20_ABI
from core.scanner import MorphoScanner
from core.rpc import RpcPool
from core.env import normalize_private_key


# ── ABI minimal du contrat MorphoKeeper déployé ──────────────────
KEEPER_ABI = [
    {
        "inputs": [
            {"name": "_poolTokenBorrowed",   "type": "address"},
            {"name": "_poolTokenCollateral", "type": "address"},
            {"name": "_borrower",            "type": "address"},
            {"name": "_debtToken",           "type": "address"},
            {"name": "_debtAmount",          "type": "uint256"},
        ],
        "name": "executeLiquidation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "withdrawToken",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "assets", "type": "address[]"}],
        "name": "withdrawAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

# ABI minimal Beefy Strategy
BEEFY_STRATEGY_ABI = [
    {"inputs": [], "name": "callReward",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lastHarvest",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "paused",
     "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "harvest",
     "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

BEEFY_VAULT_ABI_MIN = [
    {"inputs": [], "name": "strategy",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]


def _get_raw_transaction(signed_tx):
    """Extrait rawTransaction compatible avec toutes les versions eth-account."""
    if hasattr(signed_tx, "rawTransaction"):
        return signed_tx.rawTransaction
    if isinstance(signed_tx, dict) and "rawTransaction" in signed_tx:
        return signed_tx["rawTransaction"]
    if hasattr(signed_tx, "__getitem__"):
        return signed_tx[0]
    raise AttributeError(f"Cannot extract rawTransaction from {type(signed_tx)}")


class Orchestrator:
    def __init__(self, on_update: Callable = None):
        self.on_update  = on_update or (lambda **kw: None)
        self.is_running = False
        self._thread    = None

        # Web3 async
        urls = POLYGON.get("rpc_urls", []) or [POLYGON["rpc_url"]]
        self.rpc_pool = RpcPool(urls)
        self.w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_pool.current()))

        # Compte depuis PRIVATE_KEY
        pk = os.getenv("PRIVATE_KEY", "")
        pk_norm = normalize_private_key(pk)
        self.account: Optional[Account] = Account.from_key(pk_norm) if pk_norm else None

        # Contrat keeper déployé
        self.contract     = None
        self.contract_addr = SETTINGS.get("contract_address", "")
        if self.contract_addr:
            self.contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self.contract_addr),
                abi=KEEPER_ABI
            )

        # Scanner Morpho
        self.scanner = MorphoScanner(on_update=lambda **kw: self._cb(**kw))

        # État & statistiques
        self.state_file  = "data/keeper_state.json"
        self.db_file     = "data/keeper_history.db"
        self.total_exec  = 0
        self.total_profit = 0.0
        self.logs: List[str] = []
        self._exec_in_progress: set = set()
        self._fail_cooldown: Dict[str, float] = {}
        self._known_addresses: List[str] = []
        self._last_address_refresh = 0.0
        self._beefy_vaults: List[Dict] = []
        self._last_beefy_fetch = 0.0
        self._best_beefy: Optional[Dict] = None

        os.makedirs("data", exist_ok=True)
        self._init_db()
        self._load_state()

    # ── Initialisation SQLite ────────────────────────────────────
    def _init_db(self):
        conn = sqlite3.connect(self.db_file)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT,
                protocol  TEXT,
                type      TEXT,
                target    TEXT,
                tx_hash   TEXT,
                profit    REAL,
                gas_usd   REAL,
                status    TEXT,
                detail    TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _log_execution(self, protocol: str, type_: str, target: str,
                       tx_hash: str, profit: float, gas_usd: float,
                       status: str, detail: str = ""):
        try:
            conn = sqlite3.connect(self.db_file)
            conn.execute(
                "INSERT INTO executions VALUES (NULL,?,?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(), protocol, type_, target,
                 tx_hash, profit, gas_usd, status, detail)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.log(f"[DB] Erreur écriture : {e}")

    # ── Persistence ──────────────────────────────────────────────
    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    s = json.load(f)
                self.total_exec   = s.get("total_exec", 0)
                self.total_profit = s.get("total_profit", 0.0)
                self._known_addresses = s.get("known_addresses", [])
            except Exception:
                pass

    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump({
                "total_exec":       self.total_exec,
                "total_profit":     self.total_profit,
                "known_addresses":  self._known_addresses[:50_000],
                "ts":               datetime.now().isoformat(),
            }, f)

    # ── Logging ──────────────────────────────────────────────────
    def log(self, msg: str):
        ts    = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 2000:
            self.logs = self.logs[-2000:]
        print(entry)
        self.on_update(log=entry)

    def _cb(self, **kw):
        if "log" in kw:
            self.log(kw.pop("log"))
        if kw:
            self.on_update(**kw)

    def _set_rpc(self, url: str):
        if not url:
            return
        self.w3 = AsyncWeb3(AsyncHTTPProvider(url))
        if self.contract_addr:
            self.contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(self.contract_addr),
                abi=KEEPER_ABI
            )
        self.scanner.set_rpc(url)

    def _rotate_rpc(self):
        if not self.rpc_pool.has_any():
            return
        new_url = self.rpc_pool.next()
        self.log(f"[RPC] Bascule vers {new_url}")
        self._set_rpc(new_url)


    # ── Collecte adresses Morpho ─────────────────────────────────
    async def _refresh_addresses(self):
        """Rafraîchit la liste des emprunteurs Morpho toutes les 2h."""
        now = time.time()
        if now - self._last_address_refresh < 7200 and self._known_addresses:
            return
        self.log("[Morpho] Collecte des emprunteurs...")
        addrs = await self.scanner.fetch_morpho_borrowers(pages=30)
        if addrs:
            existing = set(self._known_addresses)
            new_ones = [a for a in addrs if a not in existing]
            self._known_addresses.extend(new_ones)
            self._last_address_refresh = now
            self.log(f"[Morpho] {len(self._known_addresses)} adresses connues "
                     f"(+{len(new_ones)} nouvelles)")

    # ── Récupération vaults Beefy ────────────────────────────────
    async def _fetch_beefy_vaults(self):
        """Charge la liste des vaults Beefy Polygon depuis leur API."""
        import aiohttp
        now = time.time()
        if now - self._last_beefy_fetch < 3600 and self._beefy_vaults:
            return
        try:
            connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, enable_cleanup_closed=True)
            async with aiohttp.ClientSession(connector=connector) as session:
                for attempt in range(3):
                    try:
                        async with session.get(
                            POLYGON["beefy_api"],
                            timeout=aiohttp.ClientTimeout(total=20)
                        ) as resp:
                            if resp.status == 200:
                                all_vaults = await resp.json()
                                chain = SETTINGS.get("beefy_chain", "polygon")
                                include_eol = SETTINGS.get("beefy_include_eol", False)
                                include_paused = SETTINGS.get("beefy_include_paused", False)
                                def status_ok(st):
                                    if st == "active":
                                        return True
                                    if include_eol and st == "eol":
                                        return True
                                    if include_paused and st == "paused":
                                        return True
                                    return False
                                self._beefy_vaults = [
                                    v for v in all_vaults
                                    if v.get("chain") == chain
                                    and status_ok(v.get("status"))
                                    and v.get("earnContractAddress")
                                ]
                                self._last_beefy_fetch = now
                                self.log(f"[Beefy] {len(self._beefy_vaults)} vaults {chain} actifs")
                                return
                    except (aiohttp.ClientConnectionError, ConnectionResetError) as e:
                        if attempt == 2:
                            raise e
                        await asyncio.sleep(2 ** attempt)
        except Exception as e:
            self.log(f"[Beefy] Erreur fetch API : {e}")

    async def _morpho_loop(self):
        """Boucle principale : scan → liquidation."""
        scan_interval = SETTINGS.get("scan_interval_sec", 20)

        while self.is_running:
            try:
                # Rafraîchir les adresses si nécessaire
                await self._refresh_addresses()

                if not self._known_addresses:
                    self.log("[Morpho] Aucune adresse — attente collecte...")
                    await asyncio.sleep(30)
                    continue

                # Scan via Multicall3
                result = await self.scanner.run_scan(
                    addresses       = self._known_addresses,
                    hf_threshold    = SETTINGS.get("hf_threshold", 1.0),
                    watch_threshold = SETTINGS.get("watch_threshold", 1.05),
                    min_debt_usd    = SETTINGS.get("min_debt_usd", 50.0),
                    batch_size      = SETTINGS.get("multicall_batch", 300),
                )

                self.on_update(
                    liquidatable=self.scanner.liquidatable,
                    watchlist=self.scanner.watchlist,
                    scan_result=result,
                )

                # Exécuter les liquidations rentables
                if SETTINGS.get("auto_execute", True) and self.contract:
                    for candidate in self.scanner.liquidatable[:5]:
                        if candidate["profit_est"] >= SETTINGS.get("min_profit_usd", 0.3):
                            await self._do_morpho_liquidation(candidate)
                            await asyncio.sleep(2)

                self._save_state()
                await asyncio.sleep(scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"[Morpho] Erreur boucle : {e}")
                await asyncio.sleep(15)

    # ── Worker Beefy ─────────────────────────────────────────────
    async def _beefy_loop(self):
        """Boucle harvest Beefy : vérifie callReward > gas cost."""
        beefy_interval = SETTINGS.get("beefy_interval_sec", 300)
        min_reward     = SETTINGS.get("beefy_min_reward_usd", POLYGON.get("beefy_min_reward_usd", 0.5))

        while self.is_running:
            try:
                try:
                    await self._fetch_beefy_vaults()
                except ConnectionResetError as e:
                    self.log(f"[Beefy] Connexion reset : {e}")
                    await asyncio.sleep(5)
                    continue

                harvested = 0
                best = None
                reasons = {}
                max_vaults = SETTINGS.get("beefy_max_vaults", 200)
                for vault in self._beefy_vaults[:max_vaults]:  # max vaults par cycle
                    if not self.is_running:
                        break
                    try:
                        info = await self._try_beefy_harvest(vault, min_reward)
                        if info.get("reward_usd", 0) > 0:
                            if best is None or info.get("ratio", 0) > best.get("ratio", 0):
                                best = info
                        reason = info.get("reason", "")
                        if reason:
                            reasons[reason] = reasons.get(reason, 0) + 1
                        if info.get("ok"):
                            harvested += 1
                            await asyncio.sleep(3)  # pause entre harvests
                    except Exception:
                        pass

                if harvested > 0:
                    self.log(f"[Beefy] Cycle terminé : {harvested} vaults harvestés")
                elif best is not None:
                    self._best_beefy = best
                    self.log(
                        f"[Beefy] Meilleur candidat: {best.get('name','?')} | "
                        f"reward=${best.get('reward_usd',0):.3f} | "
                        f"gas=${best.get('gas_usd',0):.3f} | "
                        f"ratio={best.get('ratio',0):.2f} | "
                        f"reason={best.get('reason','')}"
                    )

                await asyncio.sleep(beefy_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"[Beefy] Erreur boucle : {e}")
                await asyncio.sleep(30)

    # ── Liquidation Morpho ───────────────────────────────────────
    async def _do_morpho_liquidation(self, candidate: Dict) -> bool:
        if not self.account or not self.contract:
            return False

        user = candidate["address"]
        key  = f"morpho:{user.lower()}"

        if key in self._exec_in_progress:
            return False

        now = time.time()
        if self._fail_cooldown.get(key, 0) > now:
            return False

        self._exec_in_progress.add(key)
        success = False

        try:
            # Pause le scanner pour éviter les race conditions
            self.scanner.pause_requested = True
            self.log(f"[Morpho] Tentative liquidation {user[:10]}... "
                     f"HF={candidate['hf']:.4f} Dette=${candidate['debt_usd']:.0f} "
                     f"Profit estimé=${candidate['profit_est']:.2f}")

            # Pour Morpho on a besoin des poolTokens réels
            # On utilise les marchés connus depuis la config
            markets     = POLYGON.get("morpho_markets", [])
            tokens      = POLYGON.get("tokens", {})
            debt_token  = tokens.get("USDC", tokens.get("USDCe", ""))

            if not markets or not debt_token:
                self.log("[Morpho] Config marchés manquante")
                return False

            # Montant : 50% de la dette (plafond Morpho)
            debt_amount = int(candidate["debt_usd"] * 0.5 * 1e6)  # USDC = 6 décimales

            gas_price = await self.w3.eth.gas_price
            max_gas   = self.w3.to_wei(SETTINGS.get("max_gas_gwei", 300), "gwei")
            gas_price = min(int(gas_price * SETTINGS.get("gas_multiplier", 1.3)), max_gas)
            nonce     = await self.w3.eth.get_transaction_count(self.account.address)

            # Essayer les paires de marchés les plus courants
            for pool_borrowed in markets[:3]:
                for pool_collateral in markets[:3]:
                    if pool_borrowed == pool_collateral:
                        continue
                    try:
                        tx = await self.contract.functions.executeLiquidation(
                            self.w3.to_checksum_address(pool_borrowed),
                            self.w3.to_checksum_address(pool_collateral),
                            self.w3.to_checksum_address(user),
                            self.w3.to_checksum_address(debt_token),
                            debt_amount,
                        ).build_transaction({
                            "from":     self.account.address,
                            "gasPrice": gas_price,
                            "nonce":    nonce,
                            "chainId":  137,
                        })

                        # Estimer le gas — si ça revert ici, on passe à la paire suivante
                        try:
                            gas_est = await self.w3.eth.estimate_gas(tx)
                            tx["gas"] = int(gas_est * 1.2)
                        except Exception as e:
                            continue  # Paire invalide → suivante

                        signed  = self.account.sign_transaction(tx)
                        tx_hash = await self.w3.eth.send_raw_transaction(
                            _get_raw_transaction(signed)
                        )
                        receipt = await asyncio.wait_for(
                            self.w3.eth.wait_for_transaction_receipt(tx_hash),
                            timeout=120
                        )

                        if receipt["status"] == 1:
                            profit = candidate["profit_est"]
                            self.total_exec   += 1
                            self.total_profit += profit
                            tx_hex = tx_hash.hex()
                            self.log(f"[Morpho] ✅ Liquidation réussie ! "
                                     f"Profit ~${profit:.2f} | TX: {tx_hex[:16]}... "
                                     f"| {POLYGON['explorer']}{tx_hex}")
                            self._log_execution(
                                "morpho", "liquidation", user, tx_hex,
                                profit, 0.05, "success"
                            )
                            self._fail_cooldown.pop(key, None)
                            # Retirer les profits vers le wallet owner
                            await self._withdraw_profits()
                            success = True
                        else:
                            self.log(f"[Morpho] TX revertée : {tx_hash.hex()[:16]}...")

                    except Exception as e:
                        continue  # Essayer la paire suivante

            if not success:
                self.log(f"[Morpho] Toutes les paires ont échoué pour {user[:10]}...")
                self._fail_cooldown[key] = now + 300  # cooldown 5 min
                self._log_execution("morpho", "liquidation", user, "", 0, 0, "failed")

        except Exception as e:
            self.log(f"[Morpho] Erreur liquidation : {e}")
            self._fail_cooldown[key] = now + 60
        finally:
            self._exec_in_progress.discard(key)
            self.scanner.pause_requested = False

        return success

    # ── Harvest Beefy ────────────────────────────────────────────
    async def _try_beefy_harvest(self, vault: Dict, min_reward_usd: float) -> Dict:
        vault_addr = vault.get("earnContractAddress", "")
        vault_name = vault.get("name", vault_addr[:10])
        # Nettoyage pour console Windows (evite les ???)
        safe_name = "".join(c for c in vault_name if ord(c) < 128).strip() or vault_addr[:10]
        key        = f"beefy:{vault_addr.lower()}"

        result = {"ok": False, "name": safe_name, "reward_usd": 0.0, "gas_usd": 0.0, "ratio": 0.0, "reason": ""}

        if not self.account:
            result["reason"] = "no_account"
            return result

        # Cooldown : ne pas re-harvester trop t??t
        if self._fail_cooldown.get(key, 0) > time.time():
            result["reason"] = "cooldown"
            return result

        try:
            cs = self.w3.to_checksum_address(vault_addr)
            vault_contract = self.w3.eth.contract(address=cs, abi=BEEFY_VAULT_ABI_MIN)

            # R??cup??rer l'adresse de la strat??gie
            strategy_addr = await vault_contract.functions.strategy().call()
            strat = self.w3.eth.contract(
                address=self.w3.to_checksum_address(strategy_addr),
                abi=BEEFY_STRATEGY_ABI
            )

            # V??rifier si en pause
            paused = await strat.functions.paused().call()
            if paused:
                result["reason"] = "paused"
                return result

            # V??rifier le d??lai depuis le dernier harvest
            last_harvest = await strat.functions.lastHarvest().call()
            elapsed = int(time.time()) - last_harvest
            if elapsed < POLYGON.get("beefy_min_harvest_interval", 3600):
                result["reason"] = "harvest_cooldown"
                return result

            # V??rifier la r??compense disponible
            reward_raw = await strat.functions.callReward().call()
            matic_usd = float(POLYGON.get("matic_usd", 0.6))
            reward_usd = reward_raw / 1e18 * matic_usd

            # Estimation co??t gas en USD
            gas_limit = int(SETTINGS.get("beefy_gas_limit", 400_000))
            gas_mult  = float(SETTINGS.get("beefy_gas_multiplier", 1.2))
            gas_price = await self.w3.eth.gas_price
            gas_price_eff = int(gas_price * gas_mult)
            gas_usd = (gas_price_eff * gas_limit) / 1e18 * matic_usd

            min_profit_mult = float(SETTINGS.get("beefy_min_profit_mult", 1.3))
            result["reward_usd"] = float(reward_usd)
            result["gas_usd"] = float(gas_usd)
            result["ratio"] = float(reward_usd / gas_usd) if gas_usd > 0 else 0.0

            if reward_usd < min_reward_usd:
                result["reason"] = "reward_below_min"
                if SETTINGS.get("beefy_debug", False):
                    self.log(f"[Beefy] Skip {safe_name} - reward=${reward_usd:.3f} < min ${min_reward_usd:.3f}")
                return result
            if reward_usd < gas_usd * min_profit_mult:
                result["reason"] = "reward_below_gas_mult"
                if SETTINGS.get("beefy_debug", False):
                    self.log(f"[Beefy] Skip {safe_name} - reward=${reward_usd:.3f} < gas ${gas_usd:.3f} x{min_profit_mult:.2f}")
                return result

            self.log(f"[Beefy] Harvest {safe_name} - reward=${reward_usd:.3f} | gas~${gas_usd:.3f}")

            nonce     = await self.w3.eth.get_transaction_count(self.account.address)

            tx = await strat.functions.harvest().build_transaction({
                "from":     self.account.address,
                "gasPrice": gas_price_eff,
                "nonce":    nonce,
                "chainId":  137,
                "gas":      gas_limit,
            })

            signed  = self.account.sign_transaction(tx)
            tx_hash = await self.w3.eth.send_raw_transaction(_get_raw_transaction(signed))
            receipt = await asyncio.wait_for(
                self.w3.eth.wait_for_transaction_receipt(tx_hash),
                timeout=120
            )

            if receipt["status"] == 1:
                self.total_exec   += 1
                self.total_profit += reward_usd
                tx_hex = tx_hash.hex()
                self.log(f"[Beefy] ??? Harvest r??ussi ! {vault_name} "
                         f"reward=${reward_usd:.3f} | TX: {tx_hex[:16]}...")
                self._log_execution(
                    "beefy", "harvest", vault_addr, tx_hex,
                    reward_usd, 0.02, "success", vault_name
                )
                self._fail_cooldown[key] = time.time() + POLYGON.get("beefy_min_harvest_interval", 3600)
                result["ok"] = True
                result["reason"] = "harvested"
                return result
            else:
                self._fail_cooldown[key] = time.time() + 600
                result["reason"] = "tx_failed"
                return result

        except Exception as e:
            self._fail_cooldown[key] = time.time() + 300
            err = str(e)
            if "getaddrinfo" in err or "Cannot connect to host" in err or "Connection" in err:
                self._rotate_rpc()
            result["reason"] = f"error:{e}"
            return result

    async def _withdraw_profits(self):
        if not self.account or not self.contract:
            return
        tokens = list(POLYGON["tokens"].values())
        try:
            gas_price = await self.w3.eth.gas_price
            nonce     = await self.w3.eth.get_transaction_count(self.account.address)
            tx = await self.contract.functions.withdrawAll(
                [self.w3.to_checksum_address(t) for t in tokens]
            ).build_transaction({
                "from":     self.account.address,
                "gasPrice": int(gas_price * 1.2),
                "nonce":    nonce,
                "chainId":  137,
                "gas":      500_000,
            })
            signed  = self.account.sign_transaction(tx)
            tx_hash = await self.w3.eth.send_raw_transaction(_get_raw_transaction(signed))
            await asyncio.wait_for(
                self.w3.eth.wait_for_transaction_receipt(tx_hash), timeout=60
            )
            self.log(f"[Keeper] Profits retirés vers wallet owner")
        except Exception as e:
            self.log(f"[Keeper] Erreur retrait profits : {e}")

    # ── Loop principale ──────────────────────────────────────────
    async def _run_loop(self):
        """Lance les deux workers en parallèle."""
        # Handler asyncio pour éviter les crashs Proactor (WinError 10054)
        try:
            loop = asyncio.get_running_loop()
            def _exc_handler(_loop, context):
                exc = context.get("exception")
                if isinstance(exc, ConnectionResetError):
                    self.log(f"[Async] Connexion reset : {exc}")
                    return
                msg = context.get("message", "async error")
                self.log(f"[Async] {msg}")
            loop.set_exception_handler(_exc_handler)
        except Exception:
            pass

        self.log("🚀 KEEPER-ZERO démarré")
        self.log(f"   Wallet  : {self.account.address if self.account else 'MANQUANT'}")
        self.log(f"   Contrat : {self.contract_addr or 'NON DÉPLOYÉ'}")
        self.log(f"   Réseau  : Polygon (chain 137)")

        # Vérifier le pipeline Multicall avant de démarrer
        pipeline_ok = await self.scanner.verify_pipeline()
        if not pipeline_ok:
            if SETTINGS.get("morpho_optional", True) and not self.scanner.morpho_available:
                self.log("[Morpho] Contrat absent sur ce réseau — Beefy uniquement")
                await asyncio.gather(
                    self._beefy_loop(),
                )
                return
            self.log("❌ Pipeline Multicall KO — vérifier l'ABI et le RPC")
            return

        # Lancer les deux workers en parallèle
        await asyncio.gather(
            self._morpho_loop(),
            self._beefy_loop(),
        )

    def start(self):
        if self.is_running:
            return
        if not self.account:
            print("❌ PRIVATE_KEY manquante dans .env")
            return
        self.is_running = True

        def run_async():
            asyncio.run(self._run_loop())

        self._thread = threading.Thread(target=run_async, daemon=True)
        self._thread.start()

    def stop(self):
        self.is_running = False
        self._save_state()
        self.log("⏹ KEEPER-ZERO arrêté")

    def set_contract(self, address: str):
        self.contract_addr = address
        self.contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(address),
            abi=KEEPER_ABI
        )
        self.log(f"[Keeper] Contrat chargé : {address[:12]}...")

    def get_status(self) -> Dict:
        return {
            "is_running":    self.is_running,
            "total_exec":    self.total_exec,
            "total_profit":  round(self.total_profit, 4),
            "known_addrs":   len(self._known_addresses),
            "beefy_vaults":  len(self._beefy_vaults),
            "beefy_best":    self._best_beefy,
            "liquidatable":  len(self.scanner.liquidatable),
            "watchlist":     len(self.scanner.watchlist),
            "logs":          self.logs[-50:],
        }
