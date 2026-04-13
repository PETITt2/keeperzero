"""
KEEPER-ZERO — BeefyHarvester
Surveille et execute les harvests sur les vaults Beefy Finance (Polygon).

Fonctionnement :
  1. Charge la liste des vaults actifs via l'API Beefy
  2. Pour chaque vault : lit l'adresse de la stratégie sous-jacente
  3. Vérifie callReward() > seuil minimum (evite de perdre du gas)
  4. Vérifie lastHarvest + intervalle minimum (evite double harvest)
  5. Appelle harvest() sur la stratégie → reçoit un % des rewards
  6. Toutes les infos sont sauvegardées pour analyse des performances

Beefy rémunère le caller de harvest() via un "call fee"
typiquement 0.5% des récompenses du vault collectées.
Pas de contrat à déployer — le bot appelle directement les strategies.
"""
import asyncio
import aiohttp
import json
import os
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from eth_account import Account
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from config import POLYGON, SETTINGS


# ── ABIs Beefy ───────────────────────────────────────────────────
BEEFY_VAULT_ABI = [
    {
        "inputs": [],
        "name": "strategy",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "paused",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
]

BEEFY_STRATEGY_ABI = [
    {
        "inputs": [],
        "name": "callReward",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "lastHarvest",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "paused",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "harvest",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "callFeeRecipient", "type": "address"}],
        "name": "harvest",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
]


def _raw_tx(signed):
    if hasattr(signed, "rawTransaction"):
        return signed.rawTransaction
    if isinstance(signed, dict) and "rawTransaction" in signed:
        return signed["rawTransaction"]
    if hasattr(signed, "__getitem__"):
        return signed[0]
    raise AttributeError(f"Cannot extract rawTransaction from {type(signed)}")


class BeefyHarvester:
    """
    Worker de harvest Beefy Finance sur Polygon.
    Utilisé par l'Orchestrateur — instancier une seule fois.
    """

    def __init__(self, on_update: Callable = None):
        self.on_update = on_update or (lambda **kw: None)

        self.w3 = AsyncWeb3(AsyncHTTPProvider(POLYGON["rpc_url"]))

        pk = os.getenv("PRIVATE_KEY", "")
        self.account: Optional[Account] = Account.from_key(pk) if pk else None

        # Cache des vaults chargés depuis l'API
        self._vaults: List[Dict]         = []
        self._strategy_cache: Dict[str, str] = {}  # vault_addr → strategy_addr
        self._last_api_fetch   = 0.0
        self._last_harvest_ts: Dict[str, float] = {}  # strategy_addr → timestamp

        # Statistiques
        self.total_harvests    = 0
        self.total_reward_usd  = 0.0
        self.history: List[Dict] = []

        # Données persistantes
        self._state_file = "data/beefy_state.json"
        self._load_state()

    # ── Persistence ──────────────────────────────────────────────
    def _load_state(self):
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file) as f:
                    s = json.load(f)
                self.total_harvests   = s.get("total_harvests", 0)
                self.total_reward_usd = s.get("total_reward_usd", 0.0)
                self._last_harvest_ts = s.get("last_harvest_ts", {})
            except Exception:
                pass

    def _save_state(self):
        os.makedirs("data", exist_ok=True)
        with open(self._state_file, "w") as f:
            json.dump({
                "total_harvests":   self.total_harvests,
                "total_reward_usd": round(self.total_reward_usd, 6),
                "last_harvest_ts":  self._last_harvest_ts,
                "ts":               datetime.now().isoformat(),
            }, f, indent=2)

    def log(self, msg: str):
        self.on_update(log=f"[Beefy] {msg}")

    # ── Chargement des vaults via API ────────────────────────────
    async def fetch_vaults(self, force: bool = False) -> int:
        """
        Charge la liste des vaults Beefy Polygon actifs.
        Rafraîchit toutes les heures sauf si force=True.
        Retourne le nombre de vaults chargés.
        """
        now = time.time()
        if not force and now - self._last_api_fetch < 3600 and self._vaults:
            return len(self._vaults)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    POLYGON["beefy_api"],
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status != 200:
                        self.log(f"API Beefy inaccessible (status {resp.status})")
                        return 0
                    all_vaults = await resp.json(content_type=None)

            # Filtrer : Polygon uniquement, status actif, adresse présente
            self._vaults = [
                v for v in all_vaults
                if v.get("chain") in ("polygon", "matic")
                and v.get("status") == "active"
                and v.get("earnContractAddress")
                and not v.get("isGovVault", False)
            ]
            self._last_api_fetch = now
            self.log(f"{len(self._vaults)} vaults Polygon actifs chargés")
            return len(self._vaults)

        except asyncio.TimeoutError:
            self.log("Timeout API Beefy")
            return 0
        except Exception as e:
            self.log(f"Erreur fetch API : {e}")
            return 0

    # ── Résolution strategy ──────────────────────────────────────
    async def _get_strategy(self, vault_addr: str) -> Optional[str]:
        """
        Retourne l'adresse de la stratégie sous-jacente d'un vault.
        Résultat mis en cache pour éviter les appels RPC répétés.
        """
        cs = self.w3.to_checksum_address(vault_addr)
        if cs in self._strategy_cache:
            return self._strategy_cache[cs]
        try:
            vault = self.w3.eth.contract(address=cs, abi=BEEFY_VAULT_ABI)
            strat_addr = await asyncio.wait_for(
                vault.functions.strategy().call(), timeout=10
            )
            self._strategy_cache[cs] = strat_addr
            return strat_addr
        except Exception:
            return None

    # ── Vérification rentabilité ─────────────────────────────────
    async def _check_harvest_profitable(
        self, strategy_addr: str, min_reward_usd: float
    ) -> tuple:
        """
        Vérifie si harvester ce vault est rentable.
        Retourne (profitable: bool, reward_usd: float, reason: str).
        """
        try:
            cs    = self.w3.to_checksum_address(strategy_addr)
            strat = self.w3.eth.contract(address=cs, abi=BEEFY_STRATEGY_ABI)

            # 1. Vérifier si en pause
            paused = await asyncio.wait_for(
                strat.functions.paused().call(), timeout=8
            )
            if paused:
                return False, 0.0, "paused"

            # 2. Vérifier l'intervalle depuis le dernier harvest
            last_ts = self._last_harvest_ts.get(strategy_addr.lower(), 0)
            min_interval = POLYGON.get("beefy_min_harvest_interval", 3600)
            if time.time() - last_ts < min_interval:
                return False, 0.0, "too_soon"

            # 3. Vérifier lastHarvest on-chain comme double confirmation
            try:
                last_harvest_chain = await asyncio.wait_for(
                    strat.functions.lastHarvest().call(), timeout=8
                )
                elapsed_chain = int(time.time()) - last_harvest_chain
                if elapsed_chain < min_interval:
                    return False, 0.0, "too_soon_chain"
            except Exception:
                pass  # Certaines stratégies n'ont pas lastHarvest

            # 4. Vérifier la récompense disponible
            try:
                reward_raw = await asyncio.wait_for(
                    strat.functions.callReward().call(), timeout=8
                )
            except Exception:
                return False, 0.0, "no_callreward"

            # callReward est en tokens natifs (POL/MATIC, 18 décimales)
            # Estimation : 1 POL ≈ $0.45 (conservative)
            reward_matic = reward_raw / 1e18
            reward_usd   = reward_matic * 0.45

            # Coût gas estimé pour un harvest : ~200k gas × gas_price
            gas_price_wei = await self.w3.eth.gas_price
            gas_cost_wei  = gas_price_wei * 200_000
            gas_cost_usd  = float(self.w3.from_wei(gas_cost_wei, "ether")) * 0.45

            if reward_usd < min_reward_usd:
                return False, reward_usd, f"reward_too_low (${reward_usd:.4f})"

            if reward_usd < gas_cost_usd * 1.5:  # marge de sécurité 1.5x
                return False, reward_usd, f"gas_eats_profit (gas=${gas_cost_usd:.4f})"

            return True, reward_usd, "ok"

        except asyncio.TimeoutError:
            return False, 0.0, "rpc_timeout"
        except Exception as e:
            return False, 0.0, f"error:{str(e)[:30]}"

    # ── Exécution harvest ────────────────────────────────────────
    async def harvest_vault(self, vault: Dict, min_reward_usd: float = 0.5) -> bool:
        """
        Tente de harvester un vault Beefy.
        Retourne True si harvest exécuté avec succès.
        """
        if not self.account:
            return False

        vault_addr = vault.get("earnContractAddress", "")
        vault_name = vault.get("name", vault_addr[:10])
        if not vault_addr:
            return False

        # Résoudre la stratégie
        strat_addr = await self._get_strategy(vault_addr)
        if not strat_addr:
            return False

        # Vérifier la rentabilité
        profitable, reward_usd, reason = await self._check_harvest_profitable(
            strat_addr, min_reward_usd
        )
        if not profitable:
            if reason not in ("too_soon", "too_soon_chain", "paused"):
                self.log(f"Skip {vault_name[:20]} : {reason}")
            return False

        self.log(
            f"Harvest {vault_name[:25]} — reward=${reward_usd:.4f} "
            f"strat={strat_addr[:10]}..."
        )

        try:
            cs    = self.w3.to_checksum_address(strat_addr)
            strat = self.w3.eth.contract(address=cs, abi=BEEFY_STRATEGY_ABI)

            gas_price = await self.w3.eth.gas_price
            nonce     = await self.w3.eth.get_transaction_count(self.account.address)

            # Essayer harvest(callFeeRecipient) d'abord → maximise notre reward
            # Fallback sur harvest() sans argument si non supporté
            tx = None
            for harvest_fn in [
                lambda: strat.functions.harvest(
                    self.w3.to_checksum_address(self.account.address)
                ).build_transaction({
                    "from":     self.account.address,
                    "gasPrice": int(gas_price * 1.2),
                    "nonce":    nonce,
                    "chainId":  137,
                    "gas":      500_000,
                }),
                lambda: strat.functions.harvest().build_transaction({
                    "from":     self.account.address,
                    "gasPrice": int(gas_price * 1.2),
                    "nonce":    nonce,
                    "chainId":  137,
                    "gas":      500_000,
                }),
            ]:
                try:
                    tx = await harvest_fn()
                    # Estimer le gas pour détecter un éventuel revert
                    est = await self.w3.eth.estimate_gas(tx)
                    tx["gas"] = int(est * 1.2)
                    break
                except Exception:
                    tx = None
                    continue

            if tx is None:
                self.log(f"estimate_gas échoué pour {vault_name[:20]} — skip")
                return False

            signed  = self.account.sign_transaction(tx)
            tx_hash = await self.w3.eth.send_raw_transaction(_raw_tx(signed))

            receipt = await asyncio.wait_for(
                self.w3.eth.wait_for_transaction_receipt(tx_hash),
                timeout=120
            )

            if receipt["status"] == 1:
                self.total_harvests   += 1
                self.total_reward_usd += reward_usd
                tx_hex = tx_hash.hex()

                self._last_harvest_ts[strat_addr.lower()] = time.time()

                entry = {
                    "ts":         datetime.now().isoformat(),
                    "vault":      vault_name,
                    "vault_addr": vault_addr,
                    "strat":      strat_addr,
                    "reward_usd": round(reward_usd, 6),
                    "tx":         tx_hex,
                }
                self.history.append(entry)
                if len(self.history) > 500:
                    self.history = self.history[-500:]

                self.log(
                    f"✅ {vault_name[:25]} | reward=${reward_usd:.4f} "
                    f"| TX={tx_hex[:14]}... "
                    f"| {POLYGON['explorer']}{tx_hex}"
                )
                self.on_update(
                    total_harvests=self.total_harvests,
                    total_reward_usd=round(self.total_reward_usd, 4),
                )
                self._save_state()
                return True

            else:
                self.log(f"TX revertée : {vault_name[:20]}")
                return False

        except asyncio.TimeoutError:
            self.log(f"Timeout harvest {vault_name[:20]}")
            return False
        except (ConnectionResetError, OSError) as e:
            self.log(f"Connexion RPC perdue : {e}")
            return False
        except Exception as e:
            err = str(e)
            if "revert" not in err.lower():
                self.log(f"Erreur {vault_name[:20]} : {err[:60]}")
            return False

    # ── Cycle complet ────────────────────────────────────────────
    async def run_cycle(self, min_reward_usd: float = 0.5) -> Dict:
        """
        Lance un cycle complet de harvest sur tous les vaults Polygon.
        Retourne les statistiques du cycle.
        """
        await self.fetch_vaults()

        if not self._vaults:
            self.log("Aucun vault disponible — vérifier l'API Beefy")
            return {"harvested": 0, "skipped": 0, "total_vaults": 0}

        harvested = 0
        skipped   = 0
        total     = len(self._vaults)

        self.log(f"Cycle démarré : {total} vaults à vérifier")

        for i, vault in enumerate(self._vaults):
            if i % 50 == 0 and i > 0:
                self.log(f"Progression : {i}/{total} vaults vérifiés")

            try:
                ok = await self.harvest_vault(vault, min_reward_usd)
                if ok:
                    harvested += 1
                    # Pause après un harvest réussi pour laisser la chaîne respirer
                    await asyncio.sleep(5)
                else:
                    skipped += 1
            except Exception:
                skipped += 1

            # Petite pause entre chaque vault pour ne pas spammer le RPC
            await asyncio.sleep(0.1)

        result = {
            "harvested":    harvested,
            "skipped":      skipped,
            "total_vaults": total,
            "reward_cycle": round(sum(
                h["reward_usd"] for h in self.history[-harvested:]
            ) if harvested else 0, 4),
        }

        if harvested > 0:
            self.log(
                f"Cycle terminé : {harvested} harvestés / {total} vérifiés — "
                f"reward estimé=${result['reward_cycle']:.4f}"
            )
        else:
            self.log(f"Cycle terminé : 0 harvest rentable sur {total} vaults")

        return result

    # ── Statut ───────────────────────────────────────────────────
    def get_stats(self) -> Dict:
        return {
            "total_harvests":   self.total_harvests,
            "total_reward_usd": round(self.total_reward_usd, 6),
            "vaults_loaded":    len(self._vaults),
            "strategies_cached":len(self._strategy_cache),
            "last_10":          self.history[-10:],
        }
