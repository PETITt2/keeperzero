// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * KEEPER-ZERO — MorphoKeeper
 *
 * Flow liquidation Morpho :
 *   1. Bot détecte HF < 1.0 sur Morpho Optimizer (Polygon)
 *   2. Bot appelle executeLiquidation() sur ce contrat
 *   3. Ce contrat prend un flashloan AAVE v3 (debtToken)
 *   4. executeOperation() : liquide l'utilisateur sur Morpho
 *   5. Reçoit le collatéral + bonus (5-10% selon marché)
 *   6. Swap collatéral → debtToken via QuickSwap
 *   7. Rembourse flashloan AAVE
 *   8. Profit reste dans le contrat → withdrawToken() vers owner
 *
 * Déploiement :
 *   cd contracts && npx hardhat compile
 *   npx hardhat run scripts/deploy.js --network polygon
 *
 * Dépendances package.json :
 *   @aave/core-v3, @openzeppelin/contracts, @uniswap/v3-periphery
 */

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

interface IAavePool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

interface IFlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

// Interface Morpho Optimizer (AAVE v3-based, Polygon)
interface IMorpho {
    function liquidate(
        address _poolTokenBorrowed,
        address _poolTokenCollateral,
        address _borrower,
        uint256 _amount,
        bool    _stakeToken
    ) external returns (uint256 amountSeized, uint256 amountLiquidated);
}

interface ISwapRouterV3 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    struct ExactInputParams {
        bytes   path;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256);
    function exactInput(ExactInputParams calldata params) external payable returns (uint256);
}

interface ISwapRouterV2 {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

contract MorphoKeeper is IFlashLoanSimpleReceiver {

    address public immutable owner;
    IAavePool         public immutable aavePool;
    IMorpho           public immutable morpho;
    ISwapRouterV3     public immutable routerV3;
    address           public immutable routerV2;

    // QuickSwap V3 fee tiers
    uint24 private constant FEE_LOW    = 500;
    uint24 private constant FEE_MEDIUM = 3000;
    uint24 private constant FEE_HIGH   = 10000;

    event KeeperLiquidation(
        address indexed borrower,
        address poolTokenBorrowed,
        address poolTokenCollateral,
        uint256 amountLiquidated,
        uint256 amountSeized,
        uint256 profit
    );

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    constructor(
        address _aavePool,   // 0x794a61358D6845594F94dc1DB02A252b5b4814aD
        address _morpho,     // 0x9485aca5bbBE1667AD97c7fE7C4531a624C8b1ED
        address _routerV3,   // 0xf5b509bB0909a69B1c207E495f687a596C168E12 (QuickSwap V3)
        address _routerV2    // 0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff (QuickSwap V2)
    ) {
        owner     = msg.sender;
        aavePool  = IAavePool(_aavePool);
        morpho    = IMorpho(_morpho);
        routerV3  = ISwapRouterV3(_routerV3);
        routerV2  = _routerV2;
    }

    // ─────────────────────────────────────────────────────────────
    // Point d'entrée — appelé par le bot Python
    // _poolTokenBorrowed   : aToken de la dette  (ex. aPolUSDC)
    // _poolTokenCollateral : aToken du collatéral (ex. aPolWETH)
    // _borrower            : adresse à liquider
    // _debtToken           : token ERC20 sous-jacent de la dette (ex. USDC)
    // _debtAmount          : montant à liquider (max 50% de la dette)
    // ─────────────────────────────────────────────────────────────
    function executeLiquidation(
        address _poolTokenBorrowed,
        address _poolTokenCollateral,
        address _borrower,
        address _debtToken,
        uint256 _debtAmount
    ) external onlyOwner {
        bytes memory params = abi.encode(
            _poolTokenBorrowed,
            _poolTokenCollateral,
            _borrower
        );
        // Flashloan du debtToken depuis AAVE v3 (frais : 0.05%)
        aavePool.flashLoanSimple(
            address(this),
            _debtToken,
            _debtAmount,
            params,
            0
        );
    }

    // ─────────────────────────────────────────────────────────────
    // Callback AAVE — déclenché automatiquement après réception flashloan
    // ─────────────────────────────────────────────────────────────
    function executeOperation(
        address asset,      // debtToken reçu en flashloan
        uint256 amount,     // montant reçu
        uint256 premium,    // frais AAVE (0.05% du montant)
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == address(aavePool), "Caller must be AAVE Pool");
        require(initiator  == address(this),     "Initiator must be this contract");

        (
            address poolTokenBorrowed,
            address poolTokenCollateral,
            address borrower
        ) = abi.decode(params, (address, address, address));

        // 1. Approuver Morpho pour utiliser le debtToken
        IERC20(asset).approve(address(morpho), amount);

        // 2. Liquider sur Morpho — reçoit le collatéral sous-jacent + bonus
        (uint256 amountSeized, uint256 amountLiquidated) = morpho.liquidate(
            poolTokenBorrowed,
            poolTokenCollateral,
            borrower,
            amount,
            false   // stakeToken = false → recevoir le token sous-jacent
        );

        // 3. Identifier le token collatéral reçu
        // poolTokenCollateral est un aToken → le underlying est le vrai token
        // On utilise le solde réel du contrat pour ne pas rater de précision
        uint256 amountToRepay = amount + premium;

        // 4. Swap collatéral → debtToken pour rembourser le flashloan
        // On cherche d'abord le solde de tout ce qui n'est pas le debtToken
        // Le collatéral reçu est le underlying du poolTokenCollateral
        // NOTE : le mapping aToken → underlying est résolu off-chain par le bot
        // et injecté dans les params si nécessaire. Ici on utilise le solde brut.
        // Pour les marchés courants (USDC/WETH/WBTC/WPOL) les bridges suffisent.

        bool repaid = _swapToRepay(asset, amountToRepay);
        require(repaid, "Swap failed: cannot repay flashloan");

        // 5. Approuver AAVE pour prélever le remboursement
        uint256 debtBalance = IERC20(asset).balanceOf(address(this));
        require(debtBalance >= amountToRepay, "Insufficient balance after swap");
        IERC20(asset).approve(address(aavePool), amountToRepay);

        // 6. Profit = ce qui reste après remboursement
        uint256 profit = debtBalance - amountToRepay;
        emit KeeperLiquidation(
            borrower,
            poolTokenBorrowed,
            poolTokenCollateral,
            amountLiquidated,
            amountSeized,
            profit
        );

        return true;
    }

    // ─────────────────────────────────────────────────────────────
    // Swap tous les tokens non-debtToken → debtToken
    // Essaie V3 (500/3000/10000) puis V2 en fallback
    // ─────────────────────────────────────────────────────────────
    function _swapToRepay(address debtToken, uint256 minAmountOut) internal returns (bool) {
        address[6] memory knownTokens = _polygonTokens();

        for (uint i = 0; i < knownTokens.length; i++) {
            address token = knownTokens[i];
            if (token == address(0) || token == debtToken) continue;

            uint256 bal = IERC20(token).balanceOf(address(this));
            if (bal == 0) continue;

            bool ok = _trySwapV3(token, debtToken, bal, minAmountOut)
                   || _trySwapV3Bridge(token, debtToken, bal, minAmountOut)
                   || _trySwapV2(token, debtToken, bal, minAmountOut);

            if (ok && IERC20(debtToken).balanceOf(address(this)) >= minAmountOut) {
                return true;
            }
        }
        return false;
    }

    function _trySwapV3(
        address tokenIn, address tokenOut,
        uint256 amountIn, uint256 minOut
    ) internal returns (bool) {
        uint24[3] memory fees = [FEE_LOW, FEE_MEDIUM, FEE_HIGH];
        IERC20(tokenIn).approve(address(routerV3), amountIn);
        for (uint i = 0; i < 3; i++) {
            try routerV3.exactInputSingle(ISwapRouterV3.ExactInputSingleParams({
                tokenIn:           tokenIn,
                tokenOut:          tokenOut,
                fee:               fees[i],
                recipient:         address(this),
                deadline:          block.timestamp + 300,
                amountIn:          amountIn,
                amountOutMinimum:  minOut,
                sqrtPriceLimitX96: 0
            })) returns (uint256 out) {
                if (out >= minOut) return true;
            } catch {}
        }
        return false;
    }

    function _trySwapV3Bridge(
        address tokenIn, address tokenOut,
        uint256 amountIn, uint256 minOut
    ) internal returns (bool) {
        address[3] memory bridges = [
            address(0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359), // USDC
            address(0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619), // WETH
            address(0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270)  // WPOL
        ];
        uint24[3] memory fees = [FEE_LOW, FEE_MEDIUM, FEE_HIGH];
        IERC20(tokenIn).approve(address(routerV3), amountIn);
        for (uint b = 0; b < 3; b++) {
            address bridge = bridges[b];
            if (bridge == tokenIn || bridge == tokenOut) continue;
            for (uint i = 0; i < 3; i++) {
                for (uint j = 0; j < 3; j++) {
                    bytes memory path = abi.encodePacked(
                        tokenIn, fees[i], bridge, fees[j], tokenOut
                    );
                    try routerV3.exactInput(ISwapRouterV3.ExactInputParams({
                        path:             path,
                        recipient:        address(this),
                        deadline:         block.timestamp + 300,
                        amountIn:         amountIn,
                        amountOutMinimum: minOut
                    })) returns (uint256 out) {
                        if (out >= minOut) return true;
                    } catch {}
                }
            }
        }
        return false;
    }

    function _trySwapV2(
        address tokenIn, address tokenOut,
        uint256 amountIn, uint256 minOut
    ) internal returns (bool) {
        if (routerV2 == address(0)) return false;
        IERC20(tokenIn).approve(routerV2, amountIn);
        address[] memory path = new address[](2);
        path[0] = tokenIn;
        path[1] = tokenOut;
        try ISwapRouterV2(routerV2).swapExactTokensForTokens(
            amountIn, minOut, path, address(this), block.timestamp + 300
        ) returns (uint256[] memory amounts) {
            if (amounts.length > 0 && amounts[amounts.length - 1] >= minOut) return true;
        } catch {}
        return false;
    }

    function _polygonTokens() internal pure returns (address[6] memory) {
        return [
            address(0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359), // USDC
            address(0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174), // USDC.e
            address(0xc2132D05D31c914a87C6611C10748AEb04B58e8F), // USDT
            address(0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619), // WETH
            address(0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270), // WPOL/WMATIC
            address(0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6)  // WBTC
        ];
    }

    // ─────────────────────────────────────────────────────────────
    // Retrait des profits vers le owner
    // ─────────────────────────────────────────────────────────────
    function withdrawToken(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        require(bal > 0, "Nothing to withdraw");
        IERC20(token).transfer(owner, bal);
    }

    function withdrawAll(address[] calldata assets) external onlyOwner {
        for (uint i = 0; i < assets.length; i++) {
            uint256 bal = IERC20(assets[i]).balanceOf(address(this));
            if (bal > 0) IERC20(assets[i]).transfer(owner, bal);
        }
        if (address(this).balance > 0) payable(owner).transfer(address(this).balance);
    }

    function withdrawNative() external onlyOwner {
        require(address(this).balance > 0, "No native balance");
        payable(owner).transfer(address(this).balance);
    }

    receive() external payable {}
}
