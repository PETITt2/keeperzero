"""
KEEPER-ZERO — Point d'entrée
Usage : python main.py
"""
import os, sys, asyncio, signal
from dotenv import load_dotenv
load_dotenv()
from core.env import normalize_private_key, is_placeholder_rpc

def check_deps():
    missing = []
    for pkg in ["web3", "aiohttp", "eth_account", "dotenv"]:
        try: __import__(pkg)
        except ImportError: missing.append(pkg)
    if missing:
        print(f"❌ Dépendances manquantes : {', '.join(missing)}")
        print("   pip install -r requirements.txt")
        sys.exit(1)

def check_env():
    pk_raw = os.getenv("PRIVATE_KEY", "")
    if not pk_raw:
        print("❌ PRIVATE_KEY manquante dans .env")
        sys.exit(1)
    if not normalize_private_key(pk_raw):
        print("❌ PRIVATE_KEY invalide (doit être hex 64 chars, ex: 0x... )")
        sys.exit(1)
    rpc = os.getenv("POLYGON_RPC", "")
    if not rpc or is_placeholder_rpc(rpc):
        print("❌ POLYGON_RPC manquante ou invalide dans .env")
        sys.exit(1)

def main():
    check_deps()
    check_env()
    os.makedirs("data", exist_ok=True)

    from orchestrator import Orchestrator
    orch = Orchestrator()

    def handle_stop(sig, frame):
        print("\n⏹ Arrêt demandé...")
        orch.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    # Déployer le contrat si pas encore fait
    contract_addr = os.getenv("KEEPER_CONTRACT", "")
    if not contract_addr:
        print("⚠️  KEEPER_CONTRACT manquante — lancez d'abord : python deploy.py")
        sys.exit(1)

    orch.set_contract(contract_addr)
    orch.start()

    # Garder le process vivant + afficher le statut toutes les 60s
    import time
    while True:
        time.sleep(60)
        status = orch.get_status()
        print(f"\n📊 Statut — Exécutions: {status['total_exec']} | "
              f"Profit total: ${status['total_profit']:.4f} | "
              f"Adresses Morpho: {status['known_addrs']} | "
              f"Vaults Beefy: {status['beefy_vaults']}")

if __name__ == "__main__":
    main()
