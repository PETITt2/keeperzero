"""
KEEPER-ZERO — Déploiement MorphoKeeper sur Polygon
Usage : python deploy.py
"""
import asyncio, json, os, sys
import shutil
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account

from config import POLYGON
from core.env import normalize_private_key, is_placeholder_rpc

ARTIFACT_PATHS = [
    Path("artifacts/contracts/MorphoKeeper.sol/MorphoKeeper.json"),
    Path("contracts/artifacts/contracts/MorphoKeeper.sol/MorphoKeeper.json"),
    Path("contracts/artifacts/MorphoKeeper.json"),
]

def load_artifact():
    for p in ARTIFACT_PATHS:
        if p.exists():
            data = json.loads(p.read_text())
            bc   = data.get("bytecode", "")
            if not bc.startswith("0x"):
                bc = "0x" + bc
            return data.get("abi", []), bc
    return None, None





def _find_hardhat_cmd(contracts_dir: Path):
    """Retourne la commande Hardhat disponible (liste d'args) ou None."""
    # Windows: prefer hardhat.cmd / hardhat.ps1 in node_modules/.bin
    bin_dir = contracts_dir / 'node_modules' / '.bin'
    for name in ('hardhat.cmd', 'hardhat.ps1', 'hardhat'):
        local = bin_dir / name
        if local.exists():
            return [str(local), 'compile']
    # Fallback: npx
    npx = shutil.which('npx.cmd') or shutil.which('npx')
    if npx:
        return [npx, 'hardhat', 'compile']
    return None

def update_env_with_contract(address: str):
    env_path = Path('.env')
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding='utf-8', errors='replace').splitlines()
    out = []
    found = False
    for line in lines:
        if line.startswith('KEEPER_CONTRACT='):
            out.append(f'KEEPER_CONTRACT={address}')
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f'KEEPER_CONTRACT={address}')
    env_path.write_text('\n'.join(out) + '\n', encoding='utf-8')

def _raw(signed):
    if hasattr(signed, "rawTransaction"): return signed.rawTransaction
    if isinstance(signed, dict): return signed["rawTransaction"]
    return signed[0]

def main():
    pk = os.getenv("PRIVATE_KEY", "")
    if not pk:
        print("❌ PRIVATE_KEY manquante"); sys.exit(1)
    pk_norm = normalize_private_key(pk)
    if not pk_norm:
        print("❌ PRIVATE_KEY invalide (hex 64 chars)" ); sys.exit(1)

    if is_placeholder_rpc(POLYGON["rpc_url"]):
        print("❌ POLYGON_RPC invalide (mettre une vraie cle RPC)" ); sys.exit(1)

    w3  = Web3(Web3.HTTPProvider(POLYGON["rpc_url"]))
    acc = Account.from_key(pk_norm)

    if not w3.is_connected():
        print("❌ RPC Polygon inaccessible"); sys.exit(1)

    bal = w3.from_wei(w3.eth.get_balance(acc.address), "ether")
    print(f"✅ Wallet : {acc.address}")
    print(f"✅ Balance : {bal:.4f} POL")

    if bal < 0.01:
        print("❌ Balance insuffisante (min 0.01 POL)"); sys.exit(1)

    # Compiler si nécessaire
    abi, bytecode = load_artifact()
    if not bytecode:
        print("📦 Compilation du contrat...")
        import subprocess
        contracts_dir = Path(__file__).parent / "contracts"
        cmd = _find_hardhat_cmd(contracts_dir)
        if not cmd:
            print("❌ Hardhat introuvable (npx ou node_modules).")
            print("   Installe Node.js + npm, puis exécute :")
            print(r"   .\scripts\setup.ps1")
            sys.exit(1)
        result = subprocess.run(
            cmd,
            cwd=str(contracts_dir),
            capture_output=True, text=True, timeout=180
        )
        if result.returncode != 0:
            print(f"❌ Compilation échouée :\n{result.stderr}"); sys.exit(1)
        abi, bytecode = load_artifact()
        if not bytecode:
            print("❌ Artifact introuvable après compilation"); sys.exit(1)

    print("\n🚀 Déploiement MorphoKeeper sur Polygon...\n")

    Contract  = w3.eth.contract(abi=abi, bytecode=bytecode)
    gas_price = int(w3.eth.gas_price * 1.3)
    nonce     = w3.eth.get_transaction_count(acc.address)

    deploy_tx = Contract.constructor(
        POLYGON["aave_pool"],
        POLYGON["morpho"]["address"],
        POLYGON["dex_router"],
        POLYGON["dex_router_v2"],
    ).build_transaction({
        "from":     acc.address,
        "gas":      3_500_000,
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  137,
    })

    try:
        est = w3.eth.estimate_gas(deploy_tx)
        deploy_tx["gas"] = int(est * 1.3)
        print(f"⛽ Gas estimé : {est}")
    except Exception as e:
        print(f"⚠️  Estimation gas échouée : {e}")

    signed   = acc.sign_transaction(deploy_tx)
    tx_hash  = w3.eth.send_raw_transaction(_raw(signed))
    print(f"📤 TX envoyée : {tx_hash.hex()}")
    print("⏳ Attente confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt["status"] == 1:
        addr = receipt["contractAddress"]
        print(f"\n✅ Contrat déployé : {addr}")
        update_env_with_contract(addr)
        print(f"🔗 {POLYGON['explorer']}{tx_hash.hex()}\n")
        print(f"👉 Ajoute dans ton .env :")
        print(f"   KEEPER_CONTRACT={addr}\n")

        # Sauvegarder dans data/
        os.makedirs("data", exist_ok=True)
        with open("data/deployment.json", "w") as f:
            json.dump({
                "address":  addr,
                "tx_hash":  tx_hash.hex(),
                "deployer": acc.address,
                "ts":       __import__("datetime").datetime.now().isoformat(),
            }, f, indent=2)
    else:
        print("❌ Déploiement échoué (TX revertée)")
        sys.exit(1)

if __name__ == "__main__":
    main()
