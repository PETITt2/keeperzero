# KEEPER-ZERO

Bot keeper Morpho/Beefy sur Polygon.

## Démarrage rapide (Windows)

1. Copier `.env` et renseigner les valeurs réelles

```
PRIVATE_KEY=0x<64_hex>
POLYGON_RPC=https://polygon-mainnet.g.alchemy.com/v2/<CLE>
KEEPER_CONTRACT=
```

2. Installer les dépendances

```
.\scripts\setup.ps1
```

3. Déployer le contrat (remplit `KEEPER_CONTRACT` automatiquement)

```
.\scripts\deploy.ps1
```

4. Lancer le keeper

```
.\scripts\run.ps1
```

## Notes

- La clé privée doit être **hex 64 caractères** (avec ou sans `0x`).
- `deploy.py` compile via Hardhat dans `contracts/`.
- Le bot écrit ses états dans `data/`.
