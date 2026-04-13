"""
KEEPER-ZERO — Configuration centrale
Protocoles cibles : Morpho Optimizer (Polygon) + Beefy Finance (Polygon)
Capital requis : $0 — revenus via bonus keeper intégrés aux protocoles
"""
import os
from dotenv import load_dotenv
load_dotenv()

def _env(k, d=None): return os.getenv(k, d)
def _envf(k, d=0.0):
    try:
        return float(os.getenv(k, d))
    except Exception:
        return float(d)

def _env_list(k, d=""): 
    v = os.getenv(k, d)
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


# ── Multicall3 (même adresse sur toutes les EVM-chains) ──────────
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

MULTICALL3_ABI = [
    {
        "inputs": [{"components": [
            {"name": "target",       "type": "address"},
            {"name": "allowFailure", "type": "bool"},
            {"name": "callData",     "type": "bytes"}
        ], "name": "calls", "type": "tuple[]"}],
        "name": "aggregate3",
        "outputs": [{"components": [
            {"name": "success",    "type": "bool"},
            {"name": "returnData", "type": "bytes"}
        ], "name": "returnData", "type": "tuple[]"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

# ── Morpho Optimizer ABI (fonctions utiles au scanner) ───────────
# Contrat Morpho sur Polygon : 0x9485aca5bbBE1667AD97c7fE7C4531a624C8b1ED
MORPHO_ABI = [
    {
        "inputs": [{"name": "_user", "type": "address"}],
        "name": "getUserHealthFactor",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "_user", "type": "address"}],
        "name": "getUserBalanceStates",
        "outputs": [
            {"components": [
                {"name": "collateralEth",  "type": "uint256"},
                {"name": "borrowableEth",  "type": "uint256"},
                {"name": "maxDebtEth",     "type": "uint256"},
                {"name": "liquidationEth", "type": "uint256"},
                {"name": "debtEth",        "type": "uint256"},
                {"name": "healthFactor",   "type": "uint256"}
            ], "name": "", "type": "tuple"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "_poolTokenBorrowed",   "type": "address"},
            {"name": "_poolTokenCollateral", "type": "address"},
            {"name": "_borrower",            "type": "address"},
            {"name": "_amount",              "type": "uint256"},
            {"name": "_stakeToken",          "type": "bool"}
        ],
        "name": "liquidate",
        "outputs": [
            {"name": "amountSeized",   "type": "uint256"},
            {"name": "amountLiquidated","type": "uint256"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getAllMarkets",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# ── Beefy Vault ABI (fonctions utiles au harvester) ──────────────
BEEFY_VAULT_ABI = [
    {
        "inputs": [],
        "name": "earn",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
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
        "name": "strategy",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
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
        "name": "harvest",
        "outputs": [],
        "stateMutability": "nonpayable",
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
    }
]

ERC20_ABI = [
    {"inputs": [{"name": "owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"type": "uint256"}],
     "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"type": "bool"}],
     "type": "function", "stateMutability": "nonpayable"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],
     "type": "function", "stateMutability": "view"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}],
     "type": "function", "stateMutability": "view"}
]

# ── Configuration Polygon ────────────────────────────────────────
POLYGON = {
    "rpc_url":     _env("POLYGON_RPC", "https://polygon-mainnet.g.alchemy.com/v2/VOTRE_CLE"),
    "rpc_urls":    _env_list("POLYGON_RPCS", ""),
    "chain_id":    137,
    "explorer":    "https://polygonscan.com/tx/",
    "gas_token":   "POL",
    "matic_usd":  _envf("MATIC_USD", 0.6),
    "multicall":   MULTICALL3_ADDRESS,
    "data_folder": "data/polygon",

    # ── Morpho Optimizer (AAVE v3-based) ──
    "morpho": {
        "address":    _env("MORPHO_ADDRESS", "0x9485aca5bbBE1667AD97c7fE7C4531a624C8b1ED"),
        "lens":       _env("MORPHO_LENS", "0x69270da602F3f8C5B8b4ed9Ba75C7AC27Ee4E809"),
    },

    # ── AAVE v3 Pool (utilisé pour les flashloans dans MorphoKeeper) ──
    "aave_pool":   "0x794a61358D6845594F94dc1DB02A252b5b4814aD",

    # ── DEX Routers ──
    "dex_router":    "0xf5b509bB0909a69B1c207E495f687a596C168E12",  # QuickSwap V3
    "dex_router_v2": "0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff",  # QuickSwap V2

    # ── Tokens Polygon ──
    "tokens": {
        "USDC":   "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # Native USDC (Circle)
        "USDCe":  "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC.e bridgé (legacy)
        "USDT":   "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "DAI":    "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063",
        "WETH":   "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
        "WBTC":   "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6",
        "WPOL":   "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        "WMATIC": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    },

    # ── aTokens Morpho Polygon (marchés actifs) ──
    # Ce sont les adresses des aTokens AAVE v3, qui sont les "poolTokens" de Morpho
    "morpho_markets": [
        "0x6d80113e533a2C0fe82EaBD35f1875DcEA89Ea97",  # aPolUSDC
        "0xA4D94019934D8333Ef880ABFFbF2FDd611C762BD",  # aPolUSDT
        "0x82E64f49Ed5EC1bC6e43DAD4FC8Af9bb3A2312E",  # aPolDAI
        "0xe50fA9b3c56FfB159cB0FCA61F5c9D750e8128c",  # aPolWETH
        "0x078f358208685046a11C85e8ad32895DED33A249",  # aPolWBTC
        "0x6ab707Aca953eDAeFBc4fD23bA73294241490620",  # aPolWMATIC
    ],

    # ── Beefy Finance ──
    "beefy_api": "https://api.beefy.finance/vaults",
    "beefy_api_apy": "https://api.beefy.finance/apy",

    # Seuil minimal de callReward (en USD) pour déclencher un harvest
    # En dessous, le gas coûte plus que le gain
    "beefy_min_reward_usd": 0.5,

    # Intervalle minimal entre deux harvests d'un même vault (secondes)
    "beefy_min_harvest_interval": 3600,  # 1h
}

# ── Paramètres globaux ───────────────────────────────────────────
SETTINGS = {
    "scan_interval_sec":    20,      # secondes entre deux scans Morpho
    "beefy_interval_sec":   int(_env("BEEFY_INTERVAL_SEC", 120)),     # secondes entre deux checks Beefy
    "beefy_chain":          _env("BEEFY_CHAIN", "polygon"),
    "beefy_include_eol":    _env("BEEFY_INCLUDE_EOL", "0") == "1",
    "beefy_include_paused": _env("BEEFY_INCLUDE_PAUSED", "0") == "1",
    "beefy_max_vaults":     200,    # max vaults par cycle
    "beefy_gas_limit":      400000, # gas limite pour harvest
    "beefy_gas_multiplier": 1.2,    # mult gas price
    "beefy_min_reward_usd": _envf("BEEFY_MIN_REWARD_USD", 0.5),
    "beefy_min_profit_mult": _envf("BEEFY_MIN_PROFIT_MULT", 1.3),
    "beefy_debug":          _env("BEEFY_DEBUG", "0") == "1",
    "multicall_batch":      300,     # adresses par appel Multicall
    "hf_threshold":         1.0,     # HF < 1.0 → liquidable
    "watch_threshold":      1.05,    # HF < 1.05 → surveiller
    "min_debt_usd":         50,      # $50 minimum de dette pour tenter
    "min_profit_usd":       0.3,     # profit net minimum après gas
    "max_gas_gwei":         300,     # gwei max autorisé
    "gas_multiplier":       1.3,
    "contract_address":     _env("KEEPER_CONTRACT", ""),
    "auto_execute":         True,
    "morpho_optional":      True,
    "data_folder":          "data",
}
