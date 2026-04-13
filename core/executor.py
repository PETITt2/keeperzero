"""
KEEPER-ZERO — Executor
Gère l'exécution on-chain des liquidations Morpho via le contrat MorphoKeeper.

Responsabilités :
  - Construire et signer les transactions
  - Estimer le gas et vérifier la rentabilité avant envoi
  - Retry intelligent sur les paires collat/dette
  - Retrait automatique des profits après chaque succès
  - Cooldown par adresse pour éviter les tentatives répétées inutiles
"""
import asyncio
import os
import time
from typing import Callable, Dict, List, Optional

from eth_account import Account
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from config import POLYGON, SETTINGS, ERC20_ABI


# ── ABI minimal du contrat MorphoKeeper ─────────────────────────
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
    },
    {
        "inputs": [],
        "name": "withdrawNative",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
]


def _raw_tx(signed):
    """Extrait rawTransaction compatible toutes versions eth-account."""
    if hasattr(signed, "rawTransaction"):
        return signed.rawTransaction
    if isinstance(signed, dict) and "rawTransaction" in signed:
        return signed["rawTransaction"]
    if hasattr(signed, "__getitem__"):
        return signed[0]
    raise AttributeError(f"Impossible d'extraire rawTransaction de {type(signed)}")


class Executor:
    """
    Moteur d'exécution des liquidations Morpho.
    Utilisé par l'Orchestrateur — instancier une seule fois.
    """

    def __init__(self, on_update: Callable = None):
        self.on_update = on_update or (lambda **kw: None)

        self.w3 = AsyncWeb3(AsyncHTTPProvider(POLYGON["rpc_url"]))

        # Compte depuis PRIVATE_KEY
        pk = os.getenv("PRIVATE_KEY", "")
        self.account: Optional[Account] = Account.from_key(pk) if pk else None

        # Contrat keeper (chargé après déploiement via set_contract)
        self.contract      = None
        self.contract_addr = SETTINGS.get("contract_address", "")
        if self.contract_addr:
            self._load_contract(self.contract_addr)

        # Tracking état
        self._in_progress: set          = set()
        self._cooldowns: Dict[str, float] = {}

        # Statistiques
        self.total_liquidations = 0
        self.total_profit       = 0.0
        self.history: List[Dict] = []

    # ── Setup ────────────────────────────────────────────────────
    def set_contract(self, address: str):
        self.contract_addr = address
        self._load_contract(address)
        self.on_update(log=f"[Executor] Contrat chargé : {address[:12]}...")

    def _load_contract(self, address: str):
        self.contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(address),
            abi=KEEPER_ABI
        )

    def is_ready(self) -> bool:
        return self.account is not None and self.contract is not None

    # ── Estimation profit avant envoi ────────────────────────────
    async def estimate_profit(self, candidate: Dict) -> float:
        """
        Calcule le profit net estimé en USD.
        Profit = (dette * 50% * bonus 5%) − gas estimé
        """
        gas_price_wei = await self.w3.eth.gas_price
        # ~300k gas pour une liquidation Morpho avec flashloan + swap
        gas_cost_wei = gas_price_wei * 300_000
        # MATIC ≈ $0.45 (estimation conservative)
        gas_cost_usd = self.w3.from_wei(gas_cost_wei, "ether") * 0.45

        liquidatable_debt = candidate["debt_usd"] * 0.50
        profit_gross      = liquidatable_debt * 0.05
        profit_net        = float(profit_gross - float(gas_cost_usd))
        return round(profit_net, 4)

    # ── Liquidation principale ───────────────────────────────────
    async def liquidate(self, candidate: Dict) -> Optional[str]:
        """
        Exécute une liquidation Morpho.
        Retourne le tx_hash en cas de succès, None sinon.
        """
        if not self.is_ready():
            self.on_update(log="[Executor] ❌ Contrat ou wallet non configuré")
            return None

        user = candidate["address"]
        key  = f"morpho:{user.lower()}"

        # Vérifier cooldown
        if self._cooldowns.get(key, 0) > time.time():
            remaining = int(self._cooldowns[key] - time.time())
            self.on_update(log=f"[Executor] Cooldown actif {user[:10]}... ({remaining}s)")
            return None

        # Vérifier qu'une exécution n'est pas déjà en cours
        if key in self._in_progress:
            self.on_update(log=f"[Executor] Déjà en cours : {user[:10]}...")
            return None

        # Vérifier la rentabilité AVANT d'envoyer quoi que ce soit
        profit_est = await self.estimate_profit(candidate)
        min_profit = SETTINGS.get("min_profit_usd", 0.3)
        if profit_est < min_profit:
            self.on_update(
                log=f"[Executor] Profit insuffisant : ${profit_est:.4f} < ${min_profit} "
                    f"(HF={candidate['hf']:.4f} Dette=${candidate['debt_usd']:.0f})"
            )
            return None

        self._in_progress.add(key)
        self.on_update(
            log=f"[Executor] Tentative : {user[:10]}... "
                f"HF={candidate['hf']:.4f} Dette=${candidate['debt_usd']:.0f} "
                f"Profit estimé=${profit_est:.4f}"
        )

        try:
            result = await self._try_liquidation_pairs(candidate, profit_est)
            if result:
                self._cooldowns.pop(key, None)
                return result
            else:
                # Cooldown de 5 min après échec sur toutes les paires
                self._cooldowns[key] = time.time() + 300
                return None
        except Exception as e:
            self.on_update(log=f"[Executor] Erreur inattendue : {e}")
            self._cooldowns[key] = time.time() + 60
            return None
        finally:
            self._in_progress.discard(key)

    async def _try_liquidation_pairs(
        self, candidate: Dict, profit_est: float
    ) -> Optional[str]:
        """
        Essaie toutes les paires (poolTokenBorrowed, poolTokenCollateral)
        disponibles sur Morpho Polygon jusqu'à trouver celle qui fonctionne.
        """
        user    = candidate["address"]
        markets = POLYGON.get("morpho_markets", [])
        tokens  = POLYGON.get("tokens", {})

        # Tokens de dette supportés par ordre de préférence
        debt_tokens = [
            ("USDC",   tokens.get("USDC", ""),   6),
            ("USDCe",  tokens.get("USDCe", ""),  6),
            ("USDT",   tokens.get("USDT", ""),   6),
            ("DAI",    tokens.get("DAI", ""),    18),
            ("WETH",   tokens.get("WETH", ""),   18),
        ]

        gas_price = await self.w3.eth.gas_price
        max_gwei  = self.w3.to_wei(SETTINGS.get("max_gas_gwei", 300), "gwei")
        gas_price = min(int(gas_price * SETTINGS.get("gas_multiplier", 1.3)), max_gwei)
        nonce     = await self.w3.eth.get_transaction_count(self.account.address)

        attempt = 0
        for pool_borrowed in markets:
            for pool_collateral in markets:
                if pool_borrowed == pool_collateral:
                    continue
                for debt_symbol, debt_token, decimals in debt_tokens:
                    if not debt_token:
                        continue

                    attempt += 1
                    # Montant : 50% de la dette convertie dans les bons décimales
                    debt_amount = int(candidate["debt_usd"] * 0.5 * (10 ** decimals) / 1800)
                    # Note : diviser par 1800 convertit USD → ETH-like
                    # Pour USDC/USDT (6 dec) on multiplie directement
                    if decimals == 6:
                        debt_amount = int(candidate["debt_usd"] * 0.5 * 1e6)

                    self.on_update(
                        log=f"[Executor] Essai {attempt} : "
                            f"dette={debt_symbol} montant={debt_amount}"
                    )

                    tx_hash = await self._send_liquidation(
                        pool_borrowed   = pool_borrowed,
                        pool_collateral = pool_collateral,
                        borrower        = user,
                        debt_token      = debt_token,
                        debt_amount     = debt_amount,
                        gas_price       = gas_price,
                        nonce           = nonce,
                    )

                    if tx_hash:
                        self.total_liquidations += 1
                        self.total_profit       += profit_est
                        self.history.append({
                            "ts":      __import__("datetime").datetime.now().isoformat(),
                            "user":    user,
                            "tx":      tx_hash,
                            "profit":  profit_est,
                            "debt_usd":candidate["debt_usd"],
                        })
                        self.on_update(
                            log=f"[Executor] ✅ Succès ! Profit=${profit_est:.4f} "
                                f"TX={tx_hash[:16]}... "
                                f"| {POLYGON['explorer']}{tx_hash}",
                            total_liquidations=self.total_liquidations,
                            total_profit=self.total_profit,
                        )
                        # Retirer immédiatement les profits vers le wallet
                        await self.withdraw_profits()
                        return tx_hash

                    # Incrémenter le nonce pour la prochaine tentative
                    # (évite les nonce conflicts si plusieurs TX simultanées)
                    nonce += 1

        self.on_update(
            log=f"[Executor] Toutes les paires ont échoué pour {user[:10]}... "
                f"({attempt} tentatives)"
        )
        return None

    async def _send_liquidation(
        self,
        pool_borrowed: str,
        pool_collateral: str,
        borrower: str,
        debt_token: str,
        debt_amount: int,
        gas_price: int,
        nonce: int,
    ) -> Optional[str]:
        """
        Construit, signe et envoie une TX de liquidation.
        Retourne tx_hash si succès (status=1), None sinon.
        """
        try:
            tx = await self.contract.functions.executeLiquidation(
                self.w3.to_checksum_address(pool_borrowed),
                self.w3.to_checksum_address(pool_collateral),
                self.w3.to_checksum_address(borrower),
                self.w3.to_checksum_address(debt_token),
                debt_amount,
            ).build_transaction({
                "from":     self.account.address,
                "gasPrice": gas_price,
                "nonce":    nonce,
                "chainId":  137,
            })

            # estimate_gas : si ça revert ici → paire invalide, on skip
            try:
                gas_est  = await self.w3.eth.estimate_gas(tx)
                tx["gas"] = int(gas_est * 1.2)
            except Exception:
                return None   # paire invalide → silencieux

            signed  = self.account.sign_transaction(tx)
            tx_hash = await self.w3.eth.send_raw_transaction(_raw_tx(signed))

            receipt = await asyncio.wait_for(
                self.w3.eth.wait_for_transaction_receipt(tx_hash),
                timeout=120
            )

            if receipt["status"] == 1:
                return tx_hash.hex()
            else:
                self.on_update(log=f"[Executor] TX revertée : {tx_hash.hex()[:16]}...")
                return None

        except asyncio.TimeoutError:
            self.on_update(log="[Executor] Timeout attente receipt")
            return None
        except (ConnectionResetError, OSError) as e:
            self.on_update(log=f"[Executor] Connexion RPC perdue : {e}")
            return None
        except Exception as e:
            err = str(e)
            # Ne logger que les erreurs non-revert (les reverts sont attendus
            # sur les mauvaises paires et pollueraient les logs)
            if "revert" not in err.lower() and "execution reverted" not in err.lower():
                self.on_update(log=f"[Executor] Erreur TX : {err[:80]}")
            return None

    # ── Retrait automatique des profits ──────────────────────────
    async def withdraw_profits(self) -> bool:
        """
        Retire tous les tokens du contrat vers le wallet owner.
        Appelé automatiquement après chaque liquidation réussie.
        """
        if not self.is_ready():
            return False

        tokens_to_check = list(POLYGON["tokens"].values())
        tokens_with_balance = []

        # Vérifier quels tokens ont un solde non-nul dans le contrat
        for token_addr in tokens_to_check:
            try:
                token = self.w3.eth.contract(
                    address=self.w3.to_checksum_address(token_addr),
                    abi=ERC20_ABI
                )
                balance = await token.functions.balanceOf(
                    self.w3.to_checksum_address(self.contract_addr)
                ).call()
                if balance > 0:
                    tokens_with_balance.append(token_addr)
            except Exception:
                pass

        if not tokens_with_balance:
            return True

        try:
            gas_price = await self.w3.eth.gas_price
            nonce     = await self.w3.eth.get_transaction_count(self.account.address)

            tx = await self.contract.functions.withdrawAll(
                [self.w3.to_checksum_address(t) for t in tokens_with_balance]
            ).build_transaction({
                "from":     self.account.address,
                "gasPrice": int(gas_price * 1.2),
                "nonce":    nonce,
                "chainId":  137,
                "gas":      200_000 + 80_000 * len(tokens_with_balance),
            })

            signed  = self.account.sign_transaction(tx)
            tx_hash = await self.w3.eth.send_raw_transaction(_raw_tx(signed))
            receipt = await asyncio.wait_for(
                self.w3.eth.wait_for_transaction_receipt(tx_hash),
                timeout=60
            )

            if receipt["status"] == 1:
                self.on_update(
                    log=f"[Executor] Profits retirés ({len(tokens_with_balance)} tokens) "
                        f"→ {self.account.address[:10]}..."
                )
                return True
        except Exception as e:
            self.on_update(log=f"[Executor] Erreur retrait profits : {e}")

        return False

    # ── Statut ───────────────────────────────────────────────────
    def get_stats(self) -> Dict:
        return {
            "total_liquidations": self.total_liquidations,
            "total_profit_usd":   round(self.total_profit, 4),
            "in_progress":        len(self._in_progress),
            "cooldowns_active":   sum(
                1 for t in self._cooldowns.values() if t > time.time()
            ),
            "last_10":            self.history[-10:],
        }
