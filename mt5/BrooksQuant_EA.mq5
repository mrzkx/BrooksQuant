//+------------------------------------------------------------------+
//|                                              BrooksQuant_EA.mq5 |
//|                          Al Brooks Price Action Trading System  |
//|                      Ported from Python BrooksQuant v2.0        |
//+------------------------------------------------------------------+
#property copyright "BrooksQuant Team"
#property link      "https://github.com/brooksquant"
#property version   "2.00"
#property description "Al Brooks Price Action EA - MT5 Implementation"
#property description "Signals: Spike, H2/L2, Wedge, Climax, MTR, Failed Breakout"
#property strict

//+------------------------------------------------------------------+
//| Include Files                                                     |
//+------------------------------------------------------------------+
#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\OrderInfo.mqh>

//+------------------------------------------------------------------+
//| Enumerations                                                      |
//+------------------------------------------------------------------+

// 市场状态（Al Brooks 核心概念）
enum ENUM_MARKET_STATE
{
    MARKET_STATE_STRONG_TREND,    // 强趋势（禁止逆势）
    MARKET_STATE_BREAKOUT,        // 突破
    MARKET_STATE_CHANNEL,         // 通道
    MARKET_STATE_TRADING_RANGE,   // 交易区间
    MARKET_STATE_TIGHT_CHANNEL,   // 紧凑通道（禁止反转）
    MARKET_STATE_FINAL_FLAG       // 终极旗形（高胜率反转）
};

// 市场周期状态机
enum ENUM_MARKET_CYCLE
{
    MARKET_CYCLE_SPIKE,           // 尖峰阶段（Always In）
    MARKET_CYCLE_CHANNEL,         // 通道阶段
    MARKET_CYCLE_TRADING_RANGE    // 交易区间
};

// H2 状态机状态
enum ENUM_H2_STATE
{
    H2_WAITING_FOR_PULLBACK,      // 等待回调
    H2_IN_PULLBACK,               // 回调中
    H2_H1_DETECTED,               // H1已检测
    H2_WAITING_FOR_H2             // 等待H2
};

// L2 状态机状态
enum ENUM_L2_STATE
{
    L2_WAITING_FOR_BOUNCE,        // 等待反弹
    L2_IN_BOUNCE,                 // 反弹中
    L2_L1_DETECTED,               // L1已检测
    L2_WAITING_FOR_L2             // 等待L2
};

// 信号类型
enum ENUM_SIGNAL_TYPE
{
    SIGNAL_NONE,
    // Context Bypass 应急入场（最高优先级）
    SIGNAL_SPIKE_MARKET_BUY,      // SPIKE周期市价入场
    SIGNAL_SPIKE_MARKET_SELL,     // SPIKE周期市价入场
    SIGNAL_MICRO_CH_H1_BUY,       // Tight Channel H1 快速入场
    SIGNAL_MICRO_CH_H1_SELL,      // Tight Channel L1 快速入场
    SIGNAL_EMERGENCY_SPIKE_BUY,   // 极值棒下一根开盘市价多
    SIGNAL_EMERGENCY_SPIKE_SELL,  // 极值棒下一根开盘市价空
    // 标准 Spike
    SIGNAL_SPIKE_BUY,
    SIGNAL_SPIKE_SELL,
    // H2/L2 状态机信号
    SIGNAL_H1_BUY,
    SIGNAL_H2_BUY,
    SIGNAL_L1_SELL,
    SIGNAL_L2_SELL,
    // 反转信号（仅限 TRADING_RANGE 或 FINAL_FLAG）
    SIGNAL_WEDGE_BUY,
    SIGNAL_WEDGE_SELL,
    SIGNAL_CLIMAX_BUY,
    SIGNAL_CLIMAX_SELL,
    SIGNAL_MTR_BUY,
    SIGNAL_MTR_SELL,
    SIGNAL_FAILED_BO_BUY,
    SIGNAL_FAILED_BO_SELL,
    SIGNAL_GAPBAR_BUY,
    SIGNAL_GAPBAR_SELL,
    SIGNAL_FINAL_FLAG_BUY,
    SIGNAL_FINAL_FLAG_SELL
};

//+------------------------------------------------------------------+
//| Input Parameters                                                  |
//+------------------------------------------------------------------+
input group "=== 基础设置 ==="
input double   InpLotSize           = 0.02;       // 基础手数
input int      InpMagicNumber       = 20260203;  // Magic Number
input int      InpMaxPositions      = 1;         // 最大持仓数量
input bool     InpEnableTrading     = true;      // 启用实盘交易

input group "=== Al Brooks 参数 ==="
input int      InpEMAPeriod         = 20;        // EMA 周期
input int      InpATRPeriod         = 20;        // ATR 周期
input int      InpLookbackPeriod    = 20;        // 回看周期

input group "=== 信号棒质量参数 ==="
input double   InpMinBodyRatio      = 0.50;      // 最小实体占比 (0.5 = 50%)
input double   InpClosePositionPct  = 0.25;      // 收盘位置要求 (0.25 = 顶/底25%)

input group "=== 趋势检测参数 ==="
input double   InpSlopeThreshold    = 0.008;     // 强斜率阈值 (0.008 = 0.8%)
input double   InpStrongTrendScore  = 0.50;      // 强趋势得分阈值 (0-1)

input group "=== 信号控制 ==="
input int      InpSignalCooldown    = 3;         // 信号冷却期（K线数）
input bool     InpEnableSpike       = true;      // 启用 Spike 信号
input bool     InpEnableH2L2        = true;      // 启用 H2/L2 信号
input bool     InpEnableWedge       = true;      // 启用 Wedge 信号
input bool     InpEnableClimax      = true;      // 启用 Climax 信号
input bool     InpEnableMTR         = true;      // 启用 MTR 信号
input bool     InpEnableFailedBO    = true;      // 启用 Failed Breakout 信号

input group "=== V型反转 (Spike Climax) ==="
input bool     InpEnableSpikeClimax  = true;     // 启用 Spike 中的 V 型反转
input double   InpSpikeClimaxATRMult = 3.5;      // Climax 棒最小长度 (×ATR)
input double   InpReversalCoverage   = 0.60;     // 反转棒覆盖率要求 (60%)
input double   InpReversalPenetration= 0.40;     // 反转棒穿透率 (穿入 Climax 实体 40%)
input int      InpMinSpikeBars       = 3;        // Spike 最少持续 K 线数
input double   InpReversalClosePos   = 0.65;     // 反转棒收盘位置 (在强势 65% 区域)
input bool     InpRequireSecondEntry = true;     // 强趋势反转要求"第二入场" (Al Brooks: 80%第一次失败)
input int      InpSecondEntryLookback= 10;       // 第二入场：回看 K 线数（检测第一次失败）

input group "=== Context Bypass 应急入场 ==="
input bool     InpEnableSpikeMarket = true;      // 启用 Spike Market Entry
input bool     InpEnableEmergencySpike = true;   // 启用 Emergency Spike（极值棒下一根开盘市价）
input double   InpEmergencySpikeATRMult = 3.0;  // 极值棒实体最小倍数 (×ATR)
input double   InpEmergencySpikeClosePct= 0.10;  // 极强收盘：收盘在极端的比例 (10%)
input bool     InpEnableMicroChH1   = true;      // 启用 Micro Channel H1
input int      InpGapCountThreshold = 3;         // Micro Channel H1 GapCount 阈值
input int      InpHTFBypassGapCount = 5;         // HTF过滤失效的 GapCount 阈值

input group "=== 20 Gap Bar 法则 (过度延伸保护) ==="
input bool     InpEnable20GapRule   = true;      // 启用 20 Gap Bar 法则
input int      InpGapBarThreshold   = 20;        // Gap Bar 阈值（过度延伸警戒线）
input bool     InpBlockFirstPullback= true;      // 屏蔽第一次回测入场 (H1/L1)
input int      InpConsolidationBars = 5;         // 恢复条件：横盘整理最少 K 线数
input double   InpConsolidationRange= 1.5;       // 恢复条件：整理区间 ≤ X × ATR

input group "=== HTF 过滤 ==="
input ENUM_TIMEFRAMES InpHTFTimeframe = PERIOD_H1; // HTF 周期
input int      InpHTFEMAPeriod      = 20;        // HTF EMA 周期
input bool     InpEnableHTFFilter   = true;      // 启用 HTF 过滤

input group "=== 风险管理 ==="
input double   InpRiskRewardRatio   = 2.0;       // 风险回报比
input double   InpTP1Multiplier     = 0.8;       // TP1 基础倍数 (ATR 参考)
input double   InpTP2RiskMultiple   = 2.0;       // TP2 风险倍数
input double   InpTP1ClosePercent   = 50.0;      // TP1 平仓比例 (%)
input double   InpMaxStopATRMult    = 3.0;       // 最大止损 ATR 倍数

input group "=== 混合止损机制 (Hybrid Stop) ==="
input bool     InpEnableHardStop    = true;      // 启用硬止损（发送到服务器）
input double   InpHardStopBufferMult = 1.5;      // 硬止损放宽倍数（灾难保护线）
input bool     InpEnableSoftStop    = true;      // 启用软止损（收盘价逻辑止损）

input group "=== 黄金专用设置 (XAUUSD) ==="
input bool     InpEnableSpreadFilter = true;     // 启用点差过滤
input double   InpMaxSpreadMult      = 2.0;      // 最大点差倍数（相对平均）
input int      InpSpreadLookback     = 20;       // 点差回看周期
input bool     InpEnableSessionWeight = true;    // 启用时段权重
input int      InpUSSessionStart     = 14;       // 美盘开始时间 (GMT)
input int      InpUSSessionEnd       = 22;       // 美盘结束时间 (GMT)
input int      InpAsiaSessionStart   = 0;        // 亚盘开始时间 (GMT)
input int      InpAsiaSessionEnd     = 8;        // 亚盘结束时间 (GMT)

input group "=== 订单类型设置 ==="
input bool     InpUseLimitOrders    = true;      // H2/L2使用限价单
input double   InpLimitOrderOffset  = 0.0;       // 限价单偏移（点）

//+------------------------------------------------------------------+
//| Global Variables                                                  |
//+------------------------------------------------------------------+
CTrade         trade;
CPositionInfo  positionInfo;

// 技术指标句柄
int handleEMA;
int handleATR;
int handleHTFEMA;          // HTF EMA 句柄

// HTF 数据
double        g_HTFEMABuffer[];
string        g_HTFTrendDir = "";    // "up" / "down" / ""

// 市场状态
ENUM_MARKET_STATE   g_MarketState      = MARKET_STATE_CHANNEL;
ENUM_MARKET_CYCLE   g_MarketCycle      = MARKET_CYCLE_CHANNEL;
string              g_TrendDirection   = "";     // "up" / "down" / ""
double              g_TrendStrength    = 0.0;
double              g_TightChannelScore = 0.0;
string              g_TightChannelDir  = "";     // "up" / "down" / ""

// H2 状态机变量
ENUM_H2_STATE g_H2State              = H2_WAITING_FOR_PULLBACK;
double        g_H2_TrendHigh         = 0.0;
double        g_H2_PullbackStartLow  = 0.0;
double        g_H2_H1High            = 0.0;
int           g_H2_H1BarIndex        = -1;
bool          g_H2_IsStrongTrend     = false;

// L2 状态机变量
ENUM_L2_STATE g_L2State              = L2_WAITING_FOR_BOUNCE;
double        g_L2_TrendLow          = 0.0;
double        g_L2_BounceStartHigh   = 0.0;
double        g_L2_L1Low             = 0.0;
int           g_L2_L1BarIndex        = -1;
bool          g_L2_IsStrongTrend     = false;

// 信号冷却期管理
datetime      g_LastBuySignalTime    = 0;
datetime      g_LastSellSignalTime   = 0;
int           g_LastBuySignalBar     = -999;
int           g_LastSellSignalBar    = -999;

// Tight Channel 追踪
int           g_TightChannelBars     = 0;
double        g_TightChannelExtreme  = 0.0;
int           g_LastTightChannelEndBar = -1;

// GapCount 追踪（连续远离EMA的K线数）
int           g_GapCount             = 0;
double        g_GapCountExtreme      = 0.0;   // 追踪方向的极值

//+------------------------------------------------------------------+
//| 20 Gap Bar 法则 (Al Brooks: 过度延伸保护)                          |
//| 当 GapCount > 20 时，趋势已过度延伸，第一次回测 EMA 通常是陷阱       |
//+------------------------------------------------------------------+
bool          g_IsOverextended       = false;  // 是否过度延伸
bool          g_FirstPullbackBlocked = false;  // 第一次回测是否被屏蔽
string        g_OverextendDirection  = "";     // 过度延伸方向 "up" / "down"
datetime      g_OverextendStartTime  = 0;      // 过度延伸开始时间
bool          g_WaitingForRecovery   = false;  // 等待恢复（横盘整理/双底双顶）
int           g_ConsolidationCount   = 0;      // 横盘整理计数
double        g_PullbackExtreme      = 0;      // 第一次回测的极值（用于双底双顶检测）
bool          g_FirstPullbackComplete= false;  // 第一次回测是否已完成

// 状态惯性
ENUM_MARKET_STATE g_CurrentLockedState = MARKET_STATE_CHANNEL;
int           g_StateHoldBars        = 0;
int           g_LastProcessedBar     = -1;

// K线计数器（用于日志）
int           g_BarCount             = 0;

// 点差追踪（黄金保护）
double        g_SpreadHistory[];      // 点差历史
int           g_SpreadIndex          = 0;
double        g_AverageSpread        = 0;
double        g_CurrentSpread        = 0;
bool          g_SpreadFilterActive   = false;

// 时段检测
string        g_CurrentSession       = "";    // "US" / "Asia" / "EU" / "Other"
bool          g_IsSpikePreferred     = false; // Spike 信号优先
bool          g_IsRangePreferred     = false; // TradingRange 信号优先

// 品种信息
int           g_SymbolDigits         = 0;     // 小数位数
double        g_SymbolPoint          = 0;     // 最小价格单位
double        g_SymbolTickSize       = 0;     // Tick 大小
double        g_SymbolTickValue      = 0;     // Tick 价值

//=================================================================
// 混合止损机制：存储原始技术止损位
// 硬止损是放宽后的灾难保护线，软止损检查原始技术位
//=================================================================
struct SoftStopInfo
{
    ulong  ticket;           // 订单号
    double technicalSL;      // 原始技术止损位
    string side;             // "buy" or "sell"
};

SoftStopInfo g_SoftStopList[];     // 软止损列表
int          g_SoftStopCount = 0;  // 当前列表数量

// TP1 价格追踪（动态止盈触发用）
struct TP1Info
{
    ulong  ticket;
    double tp1Price;
    string side;   // "buy" / "sell"
};
TP1Info g_TP1List[];
int     g_TP1Count = 0;
#define MAX_TP1_RECORDS 32

//+------------------------------------------------------------------+
//| 反转尝试跟踪 (Al Brooks: 强趋势中第一次反转80%失败)                  |
//+------------------------------------------------------------------+
struct ReversalAttempt
{
    datetime time;           // 反转尝试时间
    double   price;          // 反转尝试的极值价格
    string   direction;      // "bullish" or "bearish"
    bool     failed;         // 是否已失败（价格突破了反转尝试的极值）
};

ReversalAttempt g_LastReversalAttempt;   // 最近一次反转尝试
bool            g_HasPendingReversal = false;  // 是否有待确认的反转尝试
int             g_ReversalAttemptCount = 0;    // 反转尝试次数（同方向）

// 缓存数组
double        g_EMABuffer[];
double        g_ATRBuffer[];
double        g_CloseBuffer[];
double        g_OpenBuffer[];
double        g_HighBuffer[];
double        g_LowBuffer[];
long          g_VolumeBuffer[];  // CopyTickVolume 需要 long 类型

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
    // 设置交易参数
    trade.SetExpertMagicNumber(InpMagicNumber);
    trade.SetDeviationInPoints(10);
    trade.SetTypeFilling(ORDER_FILLING_IOC);
    
    // 创建指标句柄
    handleEMA = iMA(_Symbol, PERIOD_CURRENT, InpEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
    handleATR = iATR(_Symbol, PERIOD_CURRENT, InpATRPeriod);
    handleHTFEMA = iMA(_Symbol, InpHTFTimeframe, InpHTFEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
    
    if(handleEMA == INVALID_HANDLE || handleATR == INVALID_HANDLE || handleHTFEMA == INVALID_HANDLE)
    {
        Print("❌ 指标初始化失败！");
        return INIT_FAILED;
    }
    
    // 设置数组为序列
    ArraySetAsSeries(g_EMABuffer, true);
    ArraySetAsSeries(g_ATRBuffer, true);
    ArraySetAsSeries(g_HTFEMABuffer, true);
    ArraySetAsSeries(g_CloseBuffer, true);
    ArraySetAsSeries(g_OpenBuffer, true);
    ArraySetAsSeries(g_HighBuffer, true);
    ArraySetAsSeries(g_LowBuffer, true);
    ArraySetAsSeries(g_VolumeBuffer, true);
    
    //=================================================================
    // 初始化品种信息（黄金适配）
    //=================================================================
    g_SymbolDigits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
    g_SymbolPoint = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    g_SymbolTickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    g_SymbolTickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
    
    Print("📊 品种信息: ", _Symbol);
    Print("   小数位数: ", g_SymbolDigits);
    Print("   Point: ", DoubleToString(g_SymbolPoint, g_SymbolDigits + 2));
    Print("   TickSize: ", DoubleToString(g_SymbolTickSize, g_SymbolDigits + 2));
    Print("   TickValue: ", DoubleToString(g_SymbolTickValue, 4));
    
    //=================================================================
    // 初始化点差历史数组
    //=================================================================
    ArrayResize(g_SpreadHistory, InpSpreadLookback);
    ArrayInitialize(g_SpreadHistory, 0);
    g_SpreadIndex = 0;
    g_AverageSpread = 0;
    
    // 初始化状态机
    ResetH2StateMachine();
    ResetL2StateMachine();
    
    // 检测是否为黄金品种
    bool isGold = (StringFind(_Symbol, "XAU") >= 0 || StringFind(_Symbol, "GOLD") >= 0);
    
    Print("✅ BrooksQuant EA 初始化成功");
    Print("   品种: ", _Symbol, isGold ? " (黄金模式)" : "");
    Print("   周期: ", EnumToString(Period()));
    Print("   EMA: ", InpEMAPeriod, " | ATR: ", InpATRPeriod);
    Print("   点差过滤: ", InpEnableSpreadFilter ? "启用" : "禁用");
    Print("   时段权重: ", InpEnableSessionWeight ? "启用" : "禁用");
    
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    // 释放指标句柄
    if(handleEMA != INVALID_HANDLE) IndicatorRelease(handleEMA);
    if(handleATR != INVALID_HANDLE) IndicatorRelease(handleATR);
    if(handleHTFEMA != INVALID_HANDLE) IndicatorRelease(handleHTFEMA);
    
    // 删除图表对象
    ObjectsDeleteAll(0, "BQ_");
    
    Print("BrooksQuant EA 已停止");
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//| 仅在新 K 线生成时执行核心逻辑扫描                                   |
//+------------------------------------------------------------------+
void OnTick()
{
    //=================================================================
    // 新 K 线检测 - 仅在新 K 线生成时执行核心逻辑
    //=================================================================
    static datetime lastBarTime = 0;
    datetime currentBarTime = iTime(_Symbol, PERIOD_CURRENT, 0);
    
    if(currentBarTime == lastBarTime)
        return; // 不是新K线，跳过核心逻辑
    
    lastBarTime = currentBarTime;
    g_BarCount++; // 递增 K 线计数器
    
    //=================================================================
    // 获取市场数据
    //=================================================================
    if(!GetMarketData())
        return;
    
    double ema = g_EMABuffer[1];  // 使用已完成的K线
    double atr = g_ATRBuffer[1];
    
    if(ema == 0 || atr == 0)
        return;
    
    //=================================================================
    // 【混合止损】检查软止损（收盘价逻辑止损）
    // 在新 K 线生成时立即检查，优先于其他逻辑
    //=================================================================
    CheckSoftStopExit();
    
    //=================================================================
    // 点差检测与更新（黄金保护）
    //=================================================================
    UpdateSpreadTracking();
    
    //=================================================================
    // 时段检测（黄金时段权重）
    //=================================================================
    UpdateSessionDetection();
    
    //=================================================================
    // 市场状态检测
    //=================================================================
    DetectMarketState(ema, atr);
    g_MarketCycle = GetMarketCycle(g_MarketState);
    int gapCount = CalculateGapCount(ema);
    
    // 20 Gap Bar 法则检测（Al Brooks: 过度延伸后第一次回测是陷阱）
    Update20GapBarRule(ema, atr);
    
    // 更新反转尝试跟踪（Al Brooks: 强趋势第一次反转 80% 失败）
    UpdateReversalAttemptTracking();
    
    //=================================================================
    // 构建上下文信息（用于日志）
    //=================================================================
    string contextBypassInfo = "";
    bool isSpikeBypass = (g_MarketCycle == MARKET_CYCLE_SPIKE && InpEnableSpikeMarket);
    bool isMicroChBypass = (g_MarketState == MARKET_STATE_TIGHT_CHANNEL && 
                            gapCount >= InpGapCountThreshold && InpEnableMicroChH1);
    bool isHTFBypass = (g_MarketState == MARKET_STATE_STRONG_TREND && 
                        gapCount >= InpHTFBypassGapCount);
    
    // 点差过滤检查（Spike_Market_Entry）
    bool spreadBlocked = false;
    if(isSpikeBypass && InpEnableSpreadFilter && g_SpreadFilterActive)
    {
        spreadBlocked = true;
        isSpikeBypass = false; // 禁用 Spike_Market_Entry
        contextBypassInfo = "⛔ Spike被点差过滤阻止(当前:" + 
                           DoubleToString(g_CurrentSpread, 1) + " > 平均×" + 
                           DoubleToString(InpMaxSpreadMult, 1) + ")";
    }
    else if(isSpikeBypass)
    {
        contextBypassInfo = "🚀 Spike_Market_Entry激活";
        // 时段权重调整
        if(InpEnableSessionWeight && g_IsSpikePreferred)
            contextBypassInfo += "(美盘加权)";
    }
    else if(isMicroChBypass)
    {
        contextBypassInfo = "🚀 Micro_Channel_H1激活(Gap=" + IntegerToString(gapCount) + ")";
    }
    else if(isHTFBypass)
    {
        contextBypassInfo = "⚡ HTF过滤失效(Gap=" + IntegerToString(gapCount) + ")";
    }
    
    // 时段权重信息
    if(InpEnableSessionWeight && g_IsRangePreferred && 
       (g_MarketState == MARKET_STATE_TRADING_RANGE))
    {
        contextBypassInfo += " | 📊 亚盘区间模式";
    }
    
    //=================================================================
    // 输出 K 线日志（与 Python 格式一致）
    //=================================================================
    PrintBarLog(gapCount, contextBypassInfo);
    
    //=================================================================
    // 信号检测（应用时段权重调整优先级）
    //=================================================================
    ENUM_SIGNAL_TYPE signal = SIGNAL_NONE;
    double stopLoss = 0;
    double baseHeight = 0;
    
    // 美盘时段：优先检测 Spike 信号
    if(InpEnableSessionWeight && g_IsSpikePreferred)
    {
        // 优先级 0: Emergency_Spike（极值棒 >3×ATR + 极强收盘，下一根开盘市价）
        if(signal == SIGNAL_NONE && InpEnableEmergencySpike)
        {
            signal = CheckEmergencySpike(ema, atr, stopLoss, baseHeight);
        }
        // 优先级 1A: SPIKE 周期 - Spike_Market_Entry（应急入场）
        if(signal == SIGNAL_NONE && isSpikeBypass && !spreadBlocked)
        {
            signal = CheckSpikeMarketEntry(ema, atr, stopLoss, baseHeight);
        }
        
        // 优先级 2: 标准 Spike
        if(signal == SIGNAL_NONE && InpEnableSpike && g_MarketCycle != MARKET_CYCLE_SPIKE)
        {
            signal = CheckSpike(ema, atr, stopLoss, baseHeight);
        }
        
        // 优先级 1B: TIGHT_CHANNEL - Micro_Channel_H1
        if(signal == SIGNAL_NONE && isMicroChBypass)
        {
            signal = CheckMicroChannelH1(ema, atr, gapCount, stopLoss, baseHeight);
        }
        
        // 优先级 3: H2/L2 状态机
        if(signal == SIGNAL_NONE && InpEnableH2L2 && g_MarketCycle != MARKET_CYCLE_SPIKE)
        {
            signal = CheckH2L2WithHTF(ema, atr, isHTFBypass, stopLoss, baseHeight);
        }
    }
    // 亚盘时段：优先检测 TradingRange 和 FailedBreakout 信号
    else if(InpEnableSessionWeight && g_IsRangePreferred)
    {
        // 优先：Failed Breakout 检测
        if(signal == SIGNAL_NONE && InpEnableFailedBO && g_MarketState == MARKET_STATE_TRADING_RANGE)
            signal = CheckFailedBreakout(ema, atr, stopLoss, baseHeight);
        
        // 优先：Wedge（区间内楔形）
        bool allowReversal = (g_MarketState == MARKET_STATE_TRADING_RANGE || 
                              g_MarketState == MARKET_STATE_FINAL_FLAG);
        if(signal == SIGNAL_NONE && InpEnableWedge && allowReversal)
            signal = CheckWedge(ema, atr, stopLoss, baseHeight);
        
        // H2/L2 状态机
        if(signal == SIGNAL_NONE && InpEnableH2L2 && g_MarketCycle != MARKET_CYCLE_SPIKE)
        {
            signal = CheckH2L2WithHTF(ema, atr, isHTFBypass, stopLoss, baseHeight);
        }
        
        // Emergency_Spike（极值棒）
        if(signal == SIGNAL_NONE && InpEnableEmergencySpike)
        {
            signal = CheckEmergencySpike(ema, atr, stopLoss, baseHeight);
        }
        // 然后是 Spike 相关
        if(signal == SIGNAL_NONE && isSpikeBypass && !spreadBlocked)
        {
            signal = CheckSpikeMarketEntry(ema, atr, stopLoss, baseHeight);
        }
        
        if(signal == SIGNAL_NONE && InpEnableSpike && g_MarketCycle != MARKET_CYCLE_SPIKE)
        {
            signal = CheckSpike(ema, atr, stopLoss, baseHeight);
        }
    }
    // 默认优先级（无时段权重或其他时段）
    else
    {
        // 优先级 0: Emergency_Spike（极值棒，下一根开盘市价）
        if(signal == SIGNAL_NONE && InpEnableEmergencySpike)
        {
            signal = CheckEmergencySpike(ema, atr, stopLoss, baseHeight);
        }
        // 优先级 1A: SPIKE 周期 - Spike_Market_Entry（应急入场）
        if(signal == SIGNAL_NONE && isSpikeBypass && !spreadBlocked)
        {
            signal = CheckSpikeMarketEntry(ema, atr, stopLoss, baseHeight);
        }
        
        // 优先级 1B: TIGHT_CHANNEL - Micro_Channel_H1（应急入场）
        if(signal == SIGNAL_NONE && isMicroChBypass)
        {
            signal = CheckMicroChannelH1(ema, atr, gapCount, stopLoss, baseHeight);
        }
        
        // 优先级 2: 标准 Spike（非 SPIKE 周期）
        if(signal == SIGNAL_NONE && InpEnableSpike && g_MarketCycle != MARKET_CYCLE_SPIKE)
        {
            signal = CheckSpike(ema, atr, stopLoss, baseHeight);
        }
        
        // 优先级 3: H2/L2 状态机
        if(signal == SIGNAL_NONE && InpEnableH2L2 && g_MarketCycle != MARKET_CYCLE_SPIKE)
        {
            signal = CheckH2L2WithHTF(ema, atr, isHTFBypass, stopLoss, baseHeight);
        }
    }
    
    //=================================================================
    // 反转信号
    //=================================================================
    bool allowReversal = (g_MarketState == MARKET_STATE_TRADING_RANGE || 
                          g_MarketState == MARKET_STATE_FINAL_FLAG);
    bool isInSpike = (g_MarketCycle == MARKET_CYCLE_SPIKE);
    
    //=================================================================
    // Climax 反转信号
    // Al Brooks 原则：
    // - Spike 阶段默认屏蔽逆势（保护新手）
    // - V 型反转是高级信号，需通过 5 道门槛才能在 Spike 触发
    //   1. Spike 持续时间 >= InpMinSpikeBars
    //   2. Climax 棒长度 >= InpSpikeClimaxATRMult × ATR
    //   3. 反转棒覆盖率 >= InpReversalCoverage
    //   4. 反转棒穿透率 >= InpReversalPenetration
    //   5. 反转棒收盘位置在强势区域
    //=================================================================
    if(signal == SIGNAL_NONE && InpEnableClimax)
    {
        if(isInSpike)
        {
            // Spike V 型反转：严格模式（5 道门槛）
            signal = CheckClimax(ema, atr, stopLoss, baseHeight, true);
        }
        else if(allowReversal)
        {
            // 正常模式：TradingRange 或 FinalFlag
            signal = CheckClimax(ema, atr, stopLoss, baseHeight, false);
        }
    }
    
    if(signal == SIGNAL_NONE && InpEnableWedge && allowReversal)
        signal = CheckWedge(ema, atr, stopLoss, baseHeight);
    
    if(signal == SIGNAL_NONE && InpEnableMTR && allowReversal)
        signal = CheckMTR(ema, atr, stopLoss, baseHeight);
    
    if(signal == SIGNAL_NONE && InpEnableFailedBO && g_MarketState == MARKET_STATE_TRADING_RANGE)
        signal = CheckFailedBreakout(ema, atr, stopLoss, baseHeight);
    
    if(signal == SIGNAL_NONE && g_MarketState == MARKET_STATE_FINAL_FLAG)
        signal = CheckFinalFlag(ema, atr, stopLoss, baseHeight);
    
    //=================================================================
    // 信号触发日志
    //=================================================================
    if(signal != SIGNAL_NONE)
    {
        PrintSignalLog(signal, stopLoss, atr);
    }
    
    //=================================================================
    // 处理信号
    //=================================================================
    if(signal != SIGNAL_NONE && stopLoss > 0)
    {
        ProcessSignal(signal, stopLoss, baseHeight);
    }
    
    //=================================================================
    // 仓位管理
    //=================================================================
    ManagePositions(ema, atr);
}

//+------------------------------------------------------------------+
//| Update Spread Tracking (点差追踪 - 黄金保护)                       |
//+------------------------------------------------------------------+
void UpdateSpreadTracking()
{
    // 获取当前点差（以点为单位）
    double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    g_CurrentSpread = (ask - bid) / g_SymbolPoint;
    
    // 更新点差历史
    if(ArraySize(g_SpreadHistory) > 0)
    {
        g_SpreadHistory[g_SpreadIndex] = g_CurrentSpread;
        g_SpreadIndex = (g_SpreadIndex + 1) % InpSpreadLookback;
        
        // 计算平均点差
        double sum = 0;
        int count = 0;
        for(int i = 0; i < InpSpreadLookback; i++)
        {
            if(g_SpreadHistory[i] > 0)
            {
                sum += g_SpreadHistory[i];
                count++;
            }
        }
        
        if(count > 0)
            g_AverageSpread = sum / count;
        else
            g_AverageSpread = g_CurrentSpread;
    }
    
    // 检查是否超过阈值
    if(g_AverageSpread > 0 && g_CurrentSpread > g_AverageSpread * InpMaxSpreadMult)
    {
        if(!g_SpreadFilterActive)
        {
            g_SpreadFilterActive = true;
            Print("⚠️ 点差过滤激活: 当前点差 ", DoubleToString(g_CurrentSpread, 1), 
                  " > 平均 ", DoubleToString(g_AverageSpread, 1), 
                  " × ", DoubleToString(InpMaxSpreadMult, 1));
        }
    }
    else
    {
        if(g_SpreadFilterActive)
        {
            g_SpreadFilterActive = false;
            Print("✅ 点差过滤解除: 当前点差 ", DoubleToString(g_CurrentSpread, 1), 
                  " <= 平均 ", DoubleToString(g_AverageSpread, 1), 
                  " × ", DoubleToString(InpMaxSpreadMult, 1));
        }
    }
}

//+------------------------------------------------------------------+
//| Update Session Detection (时段检测 - 黄金时段权重)                  |
//+------------------------------------------------------------------+
void UpdateSessionDetection()
{
    // 获取当前 GMT 时间
    datetime serverTime = TimeCurrent();
    MqlDateTime dt;
    TimeToStruct(serverTime, dt);
    
    // 获取 GMT 偏移（假设服务器时间为 GMT+0，可根据实际调整）
    // 注意：不同 broker 服务器时区可能不同，需要根据实际情况调整
    int gmtHour = dt.hour;
    
    // 检测时段
    g_CurrentSession = "";
    g_IsSpikePreferred = false;
    g_IsRangePreferred = false;
    
    // 美盘时段（14:00 - 22:00 GMT）- Spike 优先
    if(gmtHour >= InpUSSessionStart && gmtHour < InpUSSessionEnd)
    {
        g_CurrentSession = "US";
        g_IsSpikePreferred = true;
    }
    // 亚盘时段（00:00 - 08:00 GMT）- TradingRange 优先
    else if(gmtHour >= InpAsiaSessionStart && gmtHour < InpAsiaSessionEnd)
    {
        g_CurrentSession = "Asia";
        g_IsRangePreferred = true;
    }
    // 欧盘时段（08:00 - 14:00 GMT）
    else if(gmtHour >= 8 && gmtHour < 14)
    {
        g_CurrentSession = "EU";
        // 欧盘可以两者兼顾
    }
    else
    {
        g_CurrentSession = "Other";
    }
}

//+------------------------------------------------------------------+
//| Get Current Spread in Price (获取当前点差 - 以价格为单位)           |
//+------------------------------------------------------------------+
double GetCurrentSpreadPrice()
{
    double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    return ask - bid;
}

//+------------------------------------------------------------------+
//| Print Bar Log (输出 K 线日志 - 与 Python 格式一致)                  |
//+------------------------------------------------------------------+
void PrintBarLog(int gapCount, string contextBypass)
{
    // 获取 K 线数据
    double close = g_CloseBuffer[1];
    double open = g_OpenBuffer[1];
    double high = g_HighBuffer[1];
    double low = g_LowBuffer[1];
    
    // 市场状态字符串
    string stateStr = GetMarketStateString(g_MarketState);
    
    // 市场周期字符串
    string cycleStr = GetMarketCycleString(g_MarketCycle);
    
    // H2 状态字符串
    string h2Str = GetH2StateString(g_H2State);
    
    // L2 状态字符串
    string l2Str = GetL2StateString(g_L2State);
    
    // 趋势方向
    string trendStr = g_TrendDirection == "" ? "无" : (g_TrendDirection == "up" ? "↑" : "↓");
    
    // K 线类型
    string barType = "";
    if(close > open)
        barType = "阳";
    else if(close < open)
        barType = "阴";
    else
        barType = "十字";
    
    // 构建日志
    string logLine = StringFormat(
        "📊 K线#%d收盘 | %s | 状态:%s | 周期:%s | H2:%s | L2:%s | Gap:%d | 趋势:%s",
        g_BarCount,
        barType,
        stateStr,
        cycleStr,
        h2Str,
        l2Str,
        gapCount,
        trendStr
    );
    
    // 添加 20 Gap Bar 法则状态
    if(g_IsOverextended)
    {
        string gapBarStatus = "";
        if(g_WaitingForRecovery)
            gapBarStatus = "⛔ 20Gap(" + g_OverextendDirection + "):等待恢复";
        else if(g_FirstPullbackComplete)
            gapBarStatus = "✅ 20Gap:已恢复";
        else
            gapBarStatus = "⚠️ 20Gap(" + g_OverextendDirection + "):过度延伸";
        
        logLine = logLine + " | " + gapBarStatus;
    }
    
    // 添加应急逻辑信息
    if(contextBypass != "")
        logLine = logLine + " | " + contextBypass;
    
    Print(logLine);
}

//+------------------------------------------------------------------+
//| Print Signal Log (输出信号日志)                                    |
//+------------------------------------------------------------------+
void PrintSignalLog(ENUM_SIGNAL_TYPE signal, double stopLoss, double atr)
{
    string signalName = SignalTypeToString(signal);
    string side = GetSignalSide(signal);
    double entryPrice = side == "buy" ? 
                        SymbolInfoDouble(_Symbol, SYMBOL_ASK) : 
                        SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double risk = side == "buy" ? (entryPrice - stopLoss) : (stopLoss - entryPrice);
    double riskATR = atr > 0 ? risk / atr : 0;
    
    string emoji = side == "buy" ? "📈" : "📉";
    
    Print(StringFormat(
        "%s 信号触发: %s | 入场:%.5f | 止损:%.5f | 风险:%.1fATR",
        emoji,
        signalName,
        entryPrice,
        stopLoss,
        riskATR
    ));
}

//+------------------------------------------------------------------+
//| Get Market State String (获取市场状态字符串)                        |
//+------------------------------------------------------------------+
string GetMarketStateString(ENUM_MARKET_STATE state)
{
    switch(state)
    {
        case MARKET_STATE_STRONG_TREND:  return "StrongTrend";
        case MARKET_STATE_BREAKOUT:      return "Breakout";
        case MARKET_STATE_CHANNEL:       return "Channel";
        case MARKET_STATE_TRADING_RANGE: return "TradingRange";
        case MARKET_STATE_TIGHT_CHANNEL: return "TightChannel";
        case MARKET_STATE_FINAL_FLAG:    return "FinalFlag";
        default:                         return "Unknown";
    }
}

//+------------------------------------------------------------------+
//| Get Market Cycle String (获取市场周期字符串)                        |
//+------------------------------------------------------------------+
string GetMarketCycleString(ENUM_MARKET_CYCLE cycle)
{
    switch(cycle)
    {
        case MARKET_CYCLE_SPIKE:         return "Spike";
        case MARKET_CYCLE_CHANNEL:       return "Channel";
        case MARKET_CYCLE_TRADING_RANGE: return "TR";
        default:                         return "Unknown";
    }
}

//+------------------------------------------------------------------+
//| Get H2 State String (获取 H2 状态字符串)                            |
//+------------------------------------------------------------------+
string GetH2StateString(ENUM_H2_STATE state)
{
    switch(state)
    {
        case H2_WAITING_FOR_PULLBACK: return "等待回调";
        case H2_IN_PULLBACK:          return "回调中";
        case H2_H1_DETECTED:          return "H1检测";
        case H2_WAITING_FOR_H2:       return "等待H2";
        default:                      return "未知";
    }
}

//+------------------------------------------------------------------+
//| Get L2 State String (获取 L2 状态字符串)                            |
//+------------------------------------------------------------------+
string GetL2StateString(ENUM_L2_STATE state)
{
    switch(state)
    {
        case L2_WAITING_FOR_BOUNCE: return "等待反弹";
        case L2_IN_BOUNCE:          return "反弹中";
        case L2_L1_DETECTED:        return "L1检测";
        case L2_WAITING_FOR_L2:     return "等待L2";
        default:                    return "未知";
    }
}

//+------------------------------------------------------------------+
//| Get Market Data                                                   |
//+------------------------------------------------------------------+
bool GetMarketData()
{
    int required = InpLookbackPeriod + 50;
    
    // 复制指标数据
    if(CopyBuffer(handleEMA, 0, 0, required, g_EMABuffer) < required) return false;
    if(CopyBuffer(handleATR, 0, 0, required, g_ATRBuffer) < required) return false;
    
    // 复制 HTF EMA 数据
    if(CopyBuffer(handleHTFEMA, 0, 0, 10, g_HTFEMABuffer) < 5) return false;
    
    // 复制价格数据
    if(CopyClose(_Symbol, PERIOD_CURRENT, 0, required, g_CloseBuffer) < required) return false;
    if(CopyOpen(_Symbol, PERIOD_CURRENT, 0, required, g_OpenBuffer) < required) return false;
    if(CopyHigh(_Symbol, PERIOD_CURRENT, 0, required, g_HighBuffer) < required) return false;
    if(CopyLow(_Symbol, PERIOD_CURRENT, 0, required, g_LowBuffer) < required) return false;
    if(CopyTickVolume(_Symbol, PERIOD_CURRENT, 0, required, g_VolumeBuffer) < required) return false;
    
    // 更新 HTF 趋势方向
    UpdateHTFTrend();
    
    return true;
}

//+------------------------------------------------------------------+
//| Update HTF Trend Direction                                        |
//+------------------------------------------------------------------+
void UpdateHTFTrend()
{
    if(ArraySize(g_HTFEMABuffer) < 3) return;
    
    double htfEMA = g_HTFEMABuffer[1];
    double currentClose = g_CloseBuffer[1];
    
    if(currentClose > htfEMA * 1.002)
        g_HTFTrendDir = "up";
    else if(currentClose < htfEMA * 0.998)
        g_HTFTrendDir = "down";
    else
        g_HTFTrendDir = "";
}

//+------------------------------------------------------------------+
//| Calculate GapCount (连续远离EMA的K线数)                            |
//| 扩展到 50 根以支持 20 Gap Bar 法则检测                              |
//+------------------------------------------------------------------+
int CalculateGapCount(double ema)
{
    int count = 0;
    double threshold = ema * 0.002; // 0.2% 距离阈值
    
    // 检测向上 Gap
    bool checkingUp = g_CloseBuffer[1] > ema + threshold;
    bool checkingDown = g_CloseBuffer[1] < ema - threshold;
    
    if(!checkingUp && !checkingDown)
    {
        g_GapCount = 0;
        g_GapCountExtreme = 0;
        return 0;
    }
    
    // 扩展到 50 根以支持 20 Gap Bar 法则
    int maxLookback = MathMin(50, ArraySize(g_LowBuffer) - 1);
    
    for(int i = 1; i <= maxLookback; i++)
    {
        if(checkingUp)
        {
            // 整根K线都在EMA上方（低点也在EMA上方）
            if(g_LowBuffer[i] > ema)
            {
                count++;
                if(g_GapCountExtreme == 0 || g_HighBuffer[i] > g_GapCountExtreme)
                    g_GapCountExtreme = g_HighBuffer[i];
            }
            else
                break;
        }
        else if(checkingDown)
        {
            // 整根K线都在EMA下方（高点也在EMA下方）
            if(g_HighBuffer[i] < ema)
            {
                count++;
                if(g_GapCountExtreme == 0 || g_LowBuffer[i] < g_GapCountExtreme)
                    g_GapCountExtreme = g_LowBuffer[i];
            }
            else
                break;
        }
    }
    
    g_GapCount = count;
    return count;
}

//+------------------------------------------------------------------+
//| Update 20 Gap Bar Rule (Al Brooks 过度延伸保护)                    |
//| 核心原则：GapCount > 20 时，第一次回测 EMA 通常是陷阱               |
//+------------------------------------------------------------------+
void Update20GapBarRule(double ema, double atr)
{
    if(!InpEnable20GapRule) return;
    
    double threshold = ema * 0.002;
    bool priceAboveEMA = g_CloseBuffer[1] > ema + threshold;
    bool priceBelowEMA = g_CloseBuffer[1] < ema - threshold;
    bool priceTouchingEMA = !priceAboveEMA && !priceBelowEMA;
    
    //=================================================================
    // 检测过度延伸状态
    //=================================================================
    if(!g_IsOverextended && g_GapCount >= InpGapBarThreshold)
    {
        // 进入过度延伸状态
        g_IsOverextended = true;
        g_OverextendDirection = priceAboveEMA ? "up" : "down";
        g_OverextendStartTime = TimeCurrent();
        g_FirstPullbackBlocked = false;
        g_WaitingForRecovery = false;
        g_FirstPullbackComplete = false;
        g_ConsolidationCount = 0;
        g_PullbackExtreme = 0;
        
        Print("━━━━━━━━ 20 Gap Bar 法则触发 ━━━━━━━━");
        Print("   ⚠️ 趋势过度延伸: GapCount = ", g_GapCount, " >= ", InpGapBarThreshold);
        Print("   方向: ", g_OverextendDirection);
        Print("   Al Brooks: 第一次回测 EMA 通常是陷阱，屏蔽 H1/L1 入场");
    }
    
    //=================================================================
    // 过度延伸状态下的处理
    //=================================================================
    if(g_IsOverextended)
    {
        // 检测价格是否开始回测 EMA（第一次触碰）
        if(!g_FirstPullbackComplete && priceTouchingEMA)
        {
            if(!g_FirstPullbackBlocked)
            {
                g_FirstPullbackBlocked = true;
                g_WaitingForRecovery = true;
                
                // 记录回测极值（用于双底双顶检测）
                if(g_OverextendDirection == "up")
                    g_PullbackExtreme = g_LowBuffer[1];  // 上涨趋势回调的低点
                else
                    g_PullbackExtreme = g_HighBuffer[1]; // 下跌趋势反弹的高点
                
                Print("━━━━━━━━ 第一次回测 EMA 检测到 ━━━━━━━━");
                Print("   ⛔ 屏蔽第一次顺势入场 (H1/L1)");
                Print("   回测极值: ", DoubleToString(g_PullbackExtreme, g_SymbolDigits));
                Print("   等待: 横盘整理或双底/双顶形成后恢复");
            }
            
            g_ConsolidationCount++;
        }
        
        //=============================================================
        // 检测恢复条件
        //=============================================================
        if(g_WaitingForRecovery)
        {
            bool recovered = false;
            string recoveryReason = "";
            
            // 条件 1: 横盘整理（连续 N 根 K 线在窄幅区间内）
            if(g_ConsolidationCount >= InpConsolidationBars)
            {
                double rangeHigh = g_HighBuffer[1];
                double rangeLow = g_LowBuffer[1];
                
                for(int i = 2; i <= InpConsolidationBars; i++)
                {
                    if(g_HighBuffer[i] > rangeHigh) rangeHigh = g_HighBuffer[i];
                    if(g_LowBuffer[i] < rangeLow) rangeLow = g_LowBuffer[i];
                }
                
                double consolidationRange = rangeHigh - rangeLow;
                
                if(atr > 0 && consolidationRange <= atr * InpConsolidationRange)
                {
                    recovered = true;
                    recoveryReason = "横盘整理完成 (" + IntegerToString(g_ConsolidationCount) + 
                                     " 根K线, 区间=" + DoubleToString(consolidationRange / atr, 2) + "×ATR)";
                }
            }
            
            // 条件 2: 双底/双顶（价格再次测试第一次回测的极值附近）
            if(!recovered && g_PullbackExtreme > 0)
            {
                double tolerance = atr * 0.3;  // 30% ATR 容差
                
                if(g_OverextendDirection == "up")
                {
                    // 上涨趋势回调：检测双底（价格再次接近第一次回调低点）
                    if(g_LowBuffer[1] <= g_PullbackExtreme + tolerance && 
                       g_LowBuffer[1] >= g_PullbackExtreme - tolerance)
                    {
                        // 并且当前棒是阳线（多头尝试夺回）
                        if(g_CloseBuffer[1] > g_OpenBuffer[1])
                        {
                            recovered = true;
                            recoveryReason = "双底形成 (Low=" + DoubleToString(g_LowBuffer[1], g_SymbolDigits) + 
                                            " ≈ 第一次回测Low=" + DoubleToString(g_PullbackExtreme, g_SymbolDigits) + ")";
                        }
                    }
                }
                else
                {
                    // 下跌趋势反弹：检测双顶（价格再次接近第一次反弹高点）
                    if(g_HighBuffer[1] >= g_PullbackExtreme - tolerance && 
                       g_HighBuffer[1] <= g_PullbackExtreme + tolerance)
                    {
                        // 并且当前棒是阴线（空头尝试夺回）
                        if(g_CloseBuffer[1] < g_OpenBuffer[1])
                        {
                            recovered = true;
                            recoveryReason = "双顶形成 (High=" + DoubleToString(g_HighBuffer[1], g_SymbolDigits) + 
                                            " ≈ 第一次反弹High=" + DoubleToString(g_PullbackExtreme, g_SymbolDigits) + ")";
                        }
                    }
                }
            }
            
            // 条件 3: 价格完全反穿 EMA（趋势可能已反转）
            if(!recovered)
            {
                if((g_OverextendDirection == "up" && priceBelowEMA) ||
                   (g_OverextendDirection == "down" && priceAboveEMA))
                {
                    recovered = true;
                    recoveryReason = "价格穿越 EMA，趋势可能反转";
                }
            }
            
            // 执行恢复
            if(recovered)
            {
                g_FirstPullbackComplete = true;
                g_WaitingForRecovery = false;
                
                Print("━━━━━━━━ 20 Gap Bar 保护解除 ━━━━━━━━");
                Print("   ✅ 恢复原因: ", recoveryReason);
                Print("   Al Brooks: 现在可以考虑第二入场 (H2/L2)");
            }
        }
        
        //=============================================================
        // 检测过度延伸状态结束
        //=============================================================
        // GapCount 归零或方向改变，重置所有状态
        if(g_GapCount == 0 || 
           (g_OverextendDirection == "up" && priceBelowEMA) ||
           (g_OverextendDirection == "down" && priceAboveEMA))
        {
            Reset20GapBarState();
        }
    }
}

//+------------------------------------------------------------------+
//| Reset 20 Gap Bar State (重置状态)                                  |
//+------------------------------------------------------------------+
void Reset20GapBarState()
{
    if(g_IsOverextended)
    {
        Print("📊 20 Gap Bar 状态重置: 趋势延伸结束");
    }
    
    g_IsOverextended = false;
    g_FirstPullbackBlocked = false;
    g_OverextendDirection = "";
    g_OverextendStartTime = 0;
    g_WaitingForRecovery = false;
    g_ConsolidationCount = 0;
    g_PullbackExtreme = 0;
    g_FirstPullbackComplete = false;
}

//+------------------------------------------------------------------+
//| Check 20 Gap Bar Block (检查是否应屏蔽 H1/L1 信号)                  |
//| 返回 true 表示应该屏蔽                                              |
//+------------------------------------------------------------------+
bool Check20GapBarBlock(string signalType)
{
    if(!InpEnable20GapRule || !InpBlockFirstPullback)
        return false;
    
    // 只有在过度延伸且第一次回测被屏蔽、等待恢复时才屏蔽
    if(!g_IsOverextended || !g_FirstPullbackBlocked || !g_WaitingForRecovery)
        return false;
    
    // 只屏蔽 H1/L1（第一入场），不屏蔽 H2/L2（第二入场）
    if(signalType == "H1" || signalType == "L1")
    {
        // 检查信号方向是否与过度延伸方向一致（顺势）
        if((signalType == "H1" && g_OverextendDirection == "up") ||
           (signalType == "L1" && g_OverextendDirection == "down"))
        {
            Print("⛔ 20 Gap Bar 法则: 屏蔽 ", signalType, " 信号 (第一次回测陷阱)");
            Print("   等待横盘整理或双底/双顶形成后的 H2/L2");
            return true;
        }
    }
    
    return false;
}

//+------------------------------------------------------------------+
//| Detect Market State (Al Brooks 市场状态检测)                       |
//+------------------------------------------------------------------+
void DetectMarketState(double ema, double atr)
{
    // 检测强趋势
    ENUM_MARKET_STATE detectedState = MARKET_STATE_CHANNEL;
    
    // 1. 检测 Strong Trend
    if(DetectStrongTrend(ema))
    {
        detectedState = MARKET_STATE_STRONG_TREND;
    }
    // 2. 检测 Tight Channel
    else if(DetectTightChannel(ema))
    {
        detectedState = MARKET_STATE_TIGHT_CHANNEL;
        // 更新 Tight Channel 追踪
        g_TightChannelBars++;
        UpdateTightChannelTracking();
    }
    // 3. 检测 Final Flag
    else if(DetectFinalFlag(ema, atr))
    {
        detectedState = MARKET_STATE_FINAL_FLAG;
        if(g_TightChannelBars > 0)
            g_LastTightChannelEndBar = 1;
    }
    // 4. 检测 Trading Range
    else if(DetectTradingRange(ema))
    {
        detectedState = MARKET_STATE_TRADING_RANGE;
        if(g_TightChannelBars > 0)
            g_LastTightChannelEndBar = 1;
        g_TightChannelBars = 0;
    }
    // 5. 检测 Breakout
    else if(DetectBreakout(ema, atr))
    {
        detectedState = MARKET_STATE_BREAKOUT;
    }
    else
    {
        // 默认 Channel
        if(g_TightChannelBars > 0)
            g_LastTightChannelEndBar = 1;
        g_TightChannelBars = 0;
    }
    
    // 应用状态惯性
    ApplyStateInertia(detectedState);
}

//+------------------------------------------------------------------+
//| Detect Strong Trend (强趋势检测)                                   |
//+------------------------------------------------------------------+
bool DetectStrongTrend(double ema)
{
    int lookback = 10;
    
    // 统计连续同向K线
    int bullishStreak = 0;
    int bearishStreak = 0;
    int currentBullish = 0;
    int currentBearish = 0;
    int higherHighs = 0;
    int lowerLows = 0;
    int barsAboveEMA = 0;
    int barsBelowEMA = 0;
    
    for(int i = 1; i <= lookback; i++)
    {
        bool isBullish = g_CloseBuffer[i] > g_OpenBuffer[i];
        bool isBearish = g_CloseBuffer[i] < g_OpenBuffer[i];
        
        // 连续同向K线
        if(isBullish)
        {
            currentBullish++;
            currentBearish = 0;
            if(currentBullish > bullishStreak) bullishStreak = currentBullish;
        }
        else if(isBearish)
        {
            currentBearish++;
            currentBullish = 0;
            if(currentBearish > bearishStreak) bearishStreak = currentBearish;
        }
        
        // 连续创新高/新低
        if(i > 1)
        {
            if(g_HighBuffer[i] > g_HighBuffer[i+1]) higherHighs++;
            if(g_LowBuffer[i] < g_LowBuffer[i+1]) lowerLows++;
        }
        
        // EMA 位置
        if(g_CloseBuffer[i] > g_EMABuffer[i]) barsAboveEMA++;
        else barsBelowEMA++;
    }
    
    // 计算价格变化百分比
    double priceChange = 0;
    if(g_OpenBuffer[5] > 0)
        priceChange = (g_CloseBuffer[1] - g_OpenBuffer[5]) / g_OpenBuffer[5];
    
    // 计算趋势得分
    double upScore = 0;
    double downScore = 0;
    
    // 上涨趋势
    if(bullishStreak >= 3) upScore += 0.25;
    if(bullishStreak >= 5) upScore += 0.25;
    if(higherHighs >= 4) upScore += 0.2;
    if(barsAboveEMA >= 8) upScore += 0.15;
    if(priceChange > 0.008) upScore += 0.15;
    
    // 下跌趋势
    if(bearishStreak >= 3) downScore += 0.25;
    if(bearishStreak >= 5) downScore += 0.25;
    if(lowerLows >= 4) downScore += 0.2;
    if(barsBelowEMA >= 8) downScore += 0.15;
    if(priceChange < -0.008) downScore += 0.15;
    
    // 确定趋势方向
    if(upScore >= InpStrongTrendScore && upScore > downScore)
    {
        g_TrendDirection = "up";
        g_TrendStrength = upScore;
        return true;
    }
    else if(downScore >= InpStrongTrendScore && downScore > upScore)
    {
        g_TrendDirection = "down";
        g_TrendStrength = downScore;
        return true;
    }
    
    g_TrendDirection = "";
    g_TrendStrength = MathMax(upScore, downScore);
    return false;
}

//+------------------------------------------------------------------+
//| Detect Tight Channel (紧凑通道检测)                                |
//| Al Brooks: Micro Channel 可以贴着 EMA 走，关键看极值跟随             |
//+------------------------------------------------------------------+
bool DetectTightChannel(double ema)
{
    int lookback = 10;
    
    //=================================================================
    // 【条件D - 新增】极值跟随检测（Al Brooks 核心逻辑）
    // 即使 K 线触碰 EMA，只要满足极值跟随，仍视为 Tight Channel
    // - 上涨：连续 5 根 K 线，每根 Low >= 前一根 Low
    // - 下跌：连续 5 根 K 线，每根 High <= 前一根 High
    //=================================================================
    bool extremeFollowUp = CheckExtremeFollow("up", 5);
    bool extremeFollowDown = CheckExtremeFollow("down", 5);
    
    //=================================================================
    // 【条件A】所有 K 线都在 EMA 一侧（原有逻辑）
    //=================================================================
    bool allAboveEMA = true;
    bool allBelowEMA = true;
    
    for(int i = 1; i <= lookback; i++)
    {
        // 允许 0.1% 的容差（避免刚好触碰被误判）
        if(g_LowBuffer[i] <= g_EMABuffer[i] * 1.001) allAboveEMA = false;
        if(g_HighBuffer[i] >= g_EMABuffer[i] * 0.999) allBelowEMA = false;
    }
    
    //=================================================================
    // 【条件B】方向一致性（最近 5 根 K 线的阴阳比例）
    //=================================================================
    int bullishBars = 0;
    int bearishBars = 0;
    
    for(int i = 1; i <= 5; i++)
    {
        if(g_CloseBuffer[i] > g_OpenBuffer[i]) bullishBars++;
        else if(g_CloseBuffer[i] < g_OpenBuffer[i]) bearishBars++;
    }
    
    bool conditionB_Up = bullishBars >= 3;
    bool conditionB_Down = bearishBars >= 3;
    
    //=================================================================
    // 【条件C】强斜率（价格变化百分比）
    //=================================================================
    double slopePct = 0;
    if(g_CloseBuffer[lookback] > 0)
        slopePct = (g_CloseBuffer[1] - g_CloseBuffer[lookback]) / g_CloseBuffer[lookback];
    
    bool conditionC_Up = slopePct > InpSlopeThreshold;
    bool conditionC_Down = slopePct < -InpSlopeThreshold;
    
    //=================================================================
    // 综合判断（OR 关系：满足任一组合即可）
    //=================================================================
    
    // 上涨 Tight Channel 判定
    int upConditions = 0;
    if(allAboveEMA) upConditions++;
    if(conditionB_Up) upConditions++;
    if(conditionC_Up) upConditions++;
    
    // 【新增】极值跟随作为独立判定条件
    // 如果连续 5 根 K 线的 Low 都在抬升，即使触碰 EMA 也是强趋势
    bool isUpTightChannel = (upConditions >= 2) || 
                            (extremeFollowUp && conditionB_Up) ||
                            (extremeFollowUp && conditionC_Up);
    
    // 下跌 Tight Channel 判定
    int downConditions = 0;
    if(allBelowEMA) downConditions++;
    if(conditionB_Down) downConditions++;
    if(conditionC_Down) downConditions++;
    
    // 【新增】极值跟随作为独立判定条件
    bool isDownTightChannel = (downConditions >= 2) ||
                              (extremeFollowDown && conditionB_Down) ||
                              (extremeFollowDown && conditionC_Down);
    
    //=================================================================
    // 返回结果
    //=================================================================
    if(isUpTightChannel)
    {
        g_TightChannelDir = "up";
        
        // 调试日志（仅在极值跟随触发时输出）
        if(extremeFollowUp && !allAboveEMA)
        {
            Print("📈 Tight Channel UP (极值跟随): Low 连续抬升，虽触碰 EMA 但趋势未变");
        }
        return true;
    }
    else if(isDownTightChannel)
    {
        g_TightChannelDir = "down";
        
        // 调试日志
        if(extremeFollowDown && !allBelowEMA)
        {
            Print("📉 Tight Channel DOWN (极值跟随): High 连续下降，虽触碰 EMA 但趋势未变");
        }
        return true;
    }
    
    g_TightChannelDir = "";
    return false;
}

//+------------------------------------------------------------------+
//| Check Extreme Follow (极值跟随检测)                                |
//| Al Brooks: 强趋势中，K 线极值会有序跟随                             |
//| - 上涨：每根 K 线的 Low >= 前一根 Low（允许相等）                    |
//| - 下跌：每根 K 线的 High <= 前一根 High（允许相等）                  |
//+------------------------------------------------------------------+
bool CheckExtremeFollow(string direction, int barsToCheck)
{
    if(barsToCheck < 2) return false;
    
    // 确保有足够数据
    if(ArraySize(g_LowBuffer) < barsToCheck + 1 || 
       ArraySize(g_HighBuffer) < barsToCheck + 1)
        return false;
    
    if(direction == "up")
    {
        // 上涨：检查 Low 是否逐步抬升
        // bar[1] 是最新完成的 K 线，bar[barsToCheck] 是最早的
        for(int i = 1; i < barsToCheck; i++)
        {
            // 当前 K 线的 Low 不能低于前一根 K 线的 Low
            // g_LowBuffer[i] 是较新的，g_LowBuffer[i+1] 是较旧的
            if(g_LowBuffer[i] < g_LowBuffer[i + 1])
                return false;
        }
        return true;
    }
    else if(direction == "down")
    {
        // 下跌：检查 High 是否逐步下降
        for(int i = 1; i < barsToCheck; i++)
        {
            // 当前 K 线的 High 不能高于前一根 K 线的 High
            if(g_HighBuffer[i] > g_HighBuffer[i + 1])
                return false;
        }
        return true;
    }
    
    return false;
}

//+------------------------------------------------------------------+
//| Detect Trading Range (交易区间检测)                                |
//+------------------------------------------------------------------+
bool DetectTradingRange(double ema)
{
    int lookback = 20;
    int emaCrosses = 0;
    bool prevAboveEMA = g_CloseBuffer[lookback] > g_EMABuffer[lookback];
    
    for(int i = lookback - 1; i >= 1; i--)
    {
        bool currentAboveEMA = g_CloseBuffer[i] > g_EMABuffer[i];
        if(currentAboveEMA != prevAboveEMA)
        {
            emaCrosses++;
            prevAboveEMA = currentAboveEMA;
        }
    }
    
    // 穿越次数 >= 6 视为 Trading Range
    return emaCrosses >= 6;
}

//+------------------------------------------------------------------+
//| Detect Breakout (突破检测)                                         |
//+------------------------------------------------------------------+
bool DetectBreakout(double ema, double atr)
{
    // 当前K线实体大小
    double bodySize = MathAbs(g_CloseBuffer[1] - g_OpenBuffer[1]);
    
    // 计算近期平均实体
    double avgBody = 0;
    for(int i = 2; i <= 11; i++)
        avgBody += MathAbs(g_CloseBuffer[i] - g_OpenBuffer[i]);
    avgBody /= 10;
    
    // 当前实体 > 平均实体 * 1.5
    if(avgBody > 0 && bodySize > avgBody * 1.5)
    {
        double close = g_CloseBuffer[1];
        double high = g_HighBuffer[1];
        double low = g_LowBuffer[1];
        double range = high - low;
        
        if(range > 0)
        {
            // 强势收盘
            if(close > ema && (close - low) / range > 0.7)
                return true;
            if(close < ema && (high - close) / range > 0.7)
                return true;
        }
    }
    
    return false;
}

//+------------------------------------------------------------------+
//| Detect Final Flag (终极旗形检测)                                   |
//+------------------------------------------------------------------+
bool DetectFinalFlag(double ema, double atr)
{
    // 必须刚从 Tight Channel 退出
    if(g_TightChannelBars < 5) return false;
    if(g_LastTightChannelEndBar < 0) return false;
    
    int barsSinceTCEnd = g_LastTightChannelEndBar;
    if(barsSinceTCEnd < 3 || barsSinceTCEnd > 8) return false;
    
    // 价格仍远离 EMA
    double distancePct = (g_CloseBuffer[1] - ema) / ema;
    
    if(g_TightChannelDir == "up")
    {
        if(distancePct < 0.01) return false; // 距离 > 1%
    }
    else if(g_TightChannelDir == "down")
    {
        if(distancePct > -0.01) return false;
    }
    else
    {
        return false;
    }
    
    return true;
}

//+------------------------------------------------------------------+
//| Update Tight Channel Tracking                                     |
//+------------------------------------------------------------------+
void UpdateTightChannelTracking()
{
    if(g_TightChannelDir == "up")
    {
        if(g_TightChannelExtreme == 0 || g_HighBuffer[1] > g_TightChannelExtreme)
            g_TightChannelExtreme = g_HighBuffer[1];
    }
    else if(g_TightChannelDir == "down")
    {
        if(g_TightChannelExtreme == 0 || g_LowBuffer[1] < g_TightChannelExtreme)
            g_TightChannelExtreme = g_LowBuffer[1];
    }
}

//+------------------------------------------------------------------+
//| Apply State Inertia (状态惯性)                                     |
//+------------------------------------------------------------------+
void ApplyStateInertia(ENUM_MARKET_STATE newState)
{
    // 状态最小保持期
    int minHold = 1;
    switch(g_CurrentLockedState)
    {
        case MARKET_STATE_STRONG_TREND: minHold = 3; break;
        case MARKET_STATE_TIGHT_CHANNEL: minHold = 3; break;
        case MARKET_STATE_TRADING_RANGE: minHold = 2; break;
        case MARKET_STATE_BREAKOUT: minHold = 2; break;
        default: minHold = 1;
    }
    
    // 如果还在保持期内
    if(g_StateHoldBars > 0)
    {
        g_StateHoldBars--;
        g_MarketState = g_CurrentLockedState;
        return;
    }
    
    // 切换状态
    if(newState != g_CurrentLockedState)
    {
        g_CurrentLockedState = newState;
        g_StateHoldBars = minHold;
    }
    
    g_MarketState = newState;
}

//+------------------------------------------------------------------+
//| Get Market Cycle                                                  |
//+------------------------------------------------------------------+
ENUM_MARKET_CYCLE GetMarketCycle(ENUM_MARKET_STATE state)
{
    if(state == MARKET_STATE_BREAKOUT)
        return MARKET_CYCLE_SPIKE;
    else if(state == MARKET_STATE_TRADING_RANGE)
        return MARKET_CYCLE_TRADING_RANGE;
    else
        return MARKET_CYCLE_CHANNEL;
}

//+------------------------------------------------------------------+
//| Check Spike Market Entry (Context Bypass - SPIKE 周期应急入场)     |
//| 在 SPIKE 周期中，只要当前是强趋势棒，立即市价入场                   |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckSpikeMarketEntry(double ema, double atr, double &stopLoss, double &baseHeight)
{
    // 当前K线（刚收盘）
    double currHigh = g_HighBuffer[1];
    double currLow = g_LowBuffer[1];
    double currOpen = g_OpenBuffer[1];
    double currClose = g_CloseBuffer[1];
    double currBody = MathAbs(currClose - currOpen);
    double currRange = currHigh - currLow;
    
    if(currRange <= 0) return SIGNAL_NONE;
    
    double bodyRatio = currBody / currRange;
    
    // 强趋势棒条件：实体 > 60%，方向明确
    if(bodyRatio < 0.60) return SIGNAL_NONE;
    
    bool isBullish = currClose > currOpen;
    bool isBearish = currClose < currOpen;
    
    // 必须与 SPIKE 方向一致
    if(isBullish && g_TrendDirection == "up")
    {
        // 向上 SPIKE，做多
        if(!CheckSignalCooldown("buy")) return SIGNAL_NONE;
        
        // 收盘位置检查：收盘在顶部 25%
        double closePosition = (currClose - currLow) / currRange;
        if(closePosition < 0.75) return SIGNAL_NONE;
        
        stopLoss = currLow - atr * 0.3;
        
        // 检查风险
        double riskDistance = currClose - stopLoss;
        if(atr > 0 && riskDistance > atr * InpMaxStopATRMult)
            return SIGNAL_NONE;
        
        baseHeight = atr * 2.0;
        UpdateSignalCooldown("buy");
        
        Print("📈 Spike_Market_Entry BUY | GapCount: ", g_GapCount, " | Body: ", DoubleToString(bodyRatio*100, 1), "%");
        return SIGNAL_SPIKE_MARKET_BUY;
    }
    else if(isBearish && g_TrendDirection == "down")
    {
        // 向下 SPIKE，做空
        if(!CheckSignalCooldown("sell")) return SIGNAL_NONE;
        
        // 收盘位置检查：收盘在底部 25%
        double closePosition = (currHigh - currClose) / currRange;
        if(closePosition < 0.75) return SIGNAL_NONE;
        
        stopLoss = currHigh + atr * 0.3;
        
        double riskDistance = stopLoss - currClose;
        if(atr > 0 && riskDistance > atr * InpMaxStopATRMult)
            return SIGNAL_NONE;
        
        baseHeight = atr * 2.0;
        UpdateSignalCooldown("sell");
        
        Print("📉 Spike_Market_Entry SELL | GapCount: ", g_GapCount, " | Body: ", DoubleToString(bodyRatio*100, 1), "%");
        return SIGNAL_SPIKE_MARKET_SELL;
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Emergency Spike (极值棒下一根开盘市价入场)                    |
//| 极值检测：实体 > 3×ATR 且收盘在棒线极端 10% 内（极强收盘）            |
//| 提前入场：不等待 3 根确认，下一根 K 线开盘时市价入场                  |
//| 止损：信号棒 50% 位置（Al Brooks：回测超 50% 则极强棒强度不再成立）   |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckEmergencySpike(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(!InpEnableEmergencySpike || atr <= 0) return SIGNAL_NONE;
    
    // 信号棒 = 刚收盘的那根（bar[1]），下一根开盘 = 当前市价入场
    double sh = g_HighBuffer[1];
    double sl = g_LowBuffer[1];
    double so = g_OpenBuffer[1];
    double sc = g_CloseBuffer[1];
    
    double body = MathAbs(sc - so);
    double range = sh - sl;
    if(range <= 0) return SIGNAL_NONE;
    
    // 1. 极值检测：实体长度 > InpEmergencySpikeATRMult * ATR
    if(body < atr * InpEmergencySpikeATRMult)
        return SIGNAL_NONE;
    
    // 2. 极强收盘：收盘位于棒线极端的 10% 范围内
    double closeFromHigh = (sh - sc) / range;   // 0 = 收在最高，1 = 收在最低
    double closeFromLow  = (sc - sl) / range;   // 0 = 收在最低，1 = 收在最高
    
    bool isBullish = sc > so;
    bool isBearish = sc < so;
    
    // 信号棒 50% 位置（中点），用于止损
    double midpoint = sl + range * 0.5;
    double spreadPrice = GetCurrentSpreadPrice();
    
    if(isBullish)
    {
        // 阳线：收盘应在顶端 10% 内 → closeFromHigh <= 0.10
        if(closeFromHigh > InpEmergencySpikeClosePct)
            return SIGNAL_NONE;
        
        if(!CheckSignalCooldown("buy")) return SIGNAL_NONE;
        
        // 3. 止损设在信号棒 50% 下方（回测超 50% = 强度不再成立）
        stopLoss = midpoint - spreadPrice;
        stopLoss = NormalizeDouble(stopLoss, g_SymbolDigits);
        
        double riskDist = sc - stopLoss;
        if(riskDist > atr * InpMaxStopATRMult)
            return SIGNAL_NONE;
        
        baseHeight = body;
        UpdateSignalCooldown("buy");
        
        Print("🚨 Emergency_Spike BUY | Body=", DoubleToString(body/atr, 2), "×ATR | 收盘极强 ",
              DoubleToString(closeFromHigh*100, 1), "% from high | SL=信号棒50% ", DoubleToString(midpoint, g_SymbolDigits));
        return SIGNAL_EMERGENCY_SPIKE_BUY;
    }
    
    if(isBearish)
    {
        // 阴线：收盘应在底端 10% 内 → closeFromLow <= 0.10
        if(closeFromLow > InpEmergencySpikeClosePct)
            return SIGNAL_NONE;
        
        if(!CheckSignalCooldown("sell")) return SIGNAL_NONE;
        
        // 3. 止损设在信号棒 50% 上方
        stopLoss = midpoint + spreadPrice;
        stopLoss = NormalizeDouble(stopLoss, g_SymbolDigits);
        
        double riskDist = stopLoss - sc;
        if(riskDist > atr * InpMaxStopATRMult)
            return SIGNAL_NONE;
        
        baseHeight = body;
        UpdateSignalCooldown("sell");
        
        Print("🚨 Emergency_Spike SELL | Body=", DoubleToString(body/atr, 2), "×ATR | 收盘极强 ",
              DoubleToString(closeFromLow*100, 1), "% from low | SL=信号棒50% ", DoubleToString(midpoint, g_SymbolDigits));
        return SIGNAL_EMERGENCY_SPIKE_SELL;
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Micro Channel H1 (Context Bypass - TIGHT_CHANNEL 应急入场)   |
//| 在 TIGHT_CHANNEL 中，GapCount >= 3 时，突破前一棒高点立即入场       |
//| 忽略 H2 状态机的阴线计数要求                                        |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckMicroChannelH1(double ema, double atr, int gapCount, 
                                      double &stopLoss, double &baseHeight)
{
    // 当前K线
    double currHigh = g_HighBuffer[1];
    double currLow = g_LowBuffer[1];
    double currOpen = g_OpenBuffer[1];
    double currClose = g_CloseBuffer[1];
    
    // 前一K线
    double prevHigh = g_HighBuffer[2];
    double prevLow = g_LowBuffer[2];
    
    // Tight Channel 向上
    if(g_TightChannelDir == "up")
    {
        // 突破前一棒高点 -> H1 买入
        if(currHigh > prevHigh && currClose > currOpen)
        {
            if(!CheckSignalCooldown("buy")) return SIGNAL_NONE;
            
            // 不需要完整的 H2 状态机验证，直接入场
            stopLoss = MathMin(currLow, prevLow) - atr * 0.3;
            
            double riskDistance = currClose - stopLoss;
            if(atr > 0 && riskDistance > atr * InpMaxStopATRMult)
                return SIGNAL_NONE;
            
            baseHeight = atr * 2.0;
            UpdateSignalCooldown("buy");
            
            Print("🚀 Micro_Channel_H1 BUY | GapCount: ", gapCount, " | TightChannel: ", g_TightChannelBars, " bars");
            return SIGNAL_MICRO_CH_H1_BUY;
        }
    }
    // Tight Channel 向下
    else if(g_TightChannelDir == "down")
    {
        // 跌破前一棒低点 -> L1 卖出
        if(currLow < prevLow && currClose < currOpen)
        {
            if(!CheckSignalCooldown("sell")) return SIGNAL_NONE;
            
            stopLoss = MathMax(currHigh, prevHigh) + atr * 0.3;
            
            double riskDistance = stopLoss - currClose;
            if(atr > 0 && riskDistance > atr * InpMaxStopATRMult)
                return SIGNAL_NONE;
            
            baseHeight = atr * 2.0;
            UpdateSignalCooldown("sell");
            
            Print("🚀 Micro_Channel_H1 SELL | GapCount: ", gapCount, " | TightChannel: ", g_TightChannelBars, " bars");
            return SIGNAL_MICRO_CH_H1_SELL;
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check H2/L2 with HTF Filter                                       |
//| htfBypass = true 时忽略 HTF 反向过滤                               |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckH2L2WithHTF(double ema, double atr, bool htfBypass, 
                                   double &stopLoss, double &baseHeight)
{
    double close = g_CloseBuffer[1];
    double high = g_HighBuffer[1];
    double low = g_LowBuffer[1];
    
    // 更新 H2 状态机
    ENUM_SIGNAL_TYPE h2Signal = UpdateH2StateMachine(close, high, low, ema, atr, stopLoss, baseHeight);
    if(h2Signal != SIGNAL_NONE)
    {
        // HTF 过滤：除非 bypass
        if(InpEnableHTFFilter && !htfBypass)
        {
            // 买入信号需要 HTF 不是明确的 down
            if((h2Signal == SIGNAL_H1_BUY || h2Signal == SIGNAL_H2_BUY) && g_HTFTrendDir == "down")
            {
                Print("⚠️ H2 BUY blocked by HTF filter (HTF: down, GapCount: ", g_GapCount, ")");
                return SIGNAL_NONE;
            }
        }
        
        if(htfBypass && (h2Signal == SIGNAL_H1_BUY || h2Signal == SIGNAL_H2_BUY))
        {
            Print("✨ H2 BUY - HTF filter bypassed (StrongTrend + GapCount: ", g_GapCount, " >= ", InpHTFBypassGapCount, ")");
        }
        
        return h2Signal;
    }
    
    // 更新 L2 状态机
    ENUM_SIGNAL_TYPE l2Signal = UpdateL2StateMachine(close, high, low, ema, atr, stopLoss, baseHeight);
    if(l2Signal != SIGNAL_NONE)
    {
        // HTF 过滤：除非 bypass
        if(InpEnableHTFFilter && !htfBypass)
        {
            // 卖出信号需要 HTF 不是明确的 up
            if((l2Signal == SIGNAL_L1_SELL || l2Signal == SIGNAL_L2_SELL) && g_HTFTrendDir == "up")
            {
                Print("⚠️ L2 SELL blocked by HTF filter (HTF: up, GapCount: ", g_GapCount, ")");
                return SIGNAL_NONE;
            }
        }
        
        if(htfBypass && (l2Signal == SIGNAL_L1_SELL || l2Signal == SIGNAL_L2_SELL))
        {
            Print("✨ L2 SELL - HTF filter bypassed (StrongTrend + GapCount: ", g_GapCount, " >= ", InpHTFBypassGapCount, ")");
        }
        
        return l2Signal;
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Spike Signal                                                |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckSpike(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(g_MarketState != MARKET_STATE_BREAKOUT && 
       g_MarketState != MARKET_STATE_CHANNEL && 
       g_MarketState != MARKET_STATE_STRONG_TREND)
        return SIGNAL_NONE;
    
    // Signal Bar = bar[2], Entry Bar = bar[1]
    double s_high = g_HighBuffer[2];
    double s_low = g_LowBuffer[2];
    double s_open = g_OpenBuffer[2];
    double s_close = g_CloseBuffer[2];
    double s_body = MathAbs(s_close - s_open);
    double s_range = s_high - s_low;
    
    double e_close = g_CloseBuffer[1];
    double e_open = g_OpenBuffer[1];
    double e_high = g_HighBuffer[1];
    double e_low = g_LowBuffer[1];
    double e_body = MathAbs(e_close - e_open);
    double e_range = e_high - e_low;
    
    if(s_range <= 0 || e_range <= 0)
        return SIGNAL_NONE;
    
    // 过去10根的最高/最低
    double max10High = g_HighBuffer[3];
    double min10Low = g_LowBuffer[3];
    for(int i = 3; i <= 12; i++)
    {
        if(g_HighBuffer[i] > max10High) max10High = g_HighBuffer[i];
        if(g_LowBuffer[i] < min10Low) min10Low = g_LowBuffer[i];
    }
    
    // 向上 Spike
    if(s_close > s_open && e_close > e_open)
    {
        double signalBodyRatio = s_body / s_range;
        double entryBodyRatio = e_body / e_range;
        
        if(signalBodyRatio > 0.65 && entryBodyRatio > 0.50 && s_high > max10High && e_close > ema)
        {
            // 检查冷却期
            if(!CheckSignalCooldown("buy")) return SIGNAL_NONE;
            
            // 检查趋势方向过滤
            if(g_MarketState == MARKET_STATE_STRONG_TREND && g_TrendDirection == "down")
                return SIGNAL_NONE;
            
            // 止损：Signal Bar 低点外
            stopLoss = s_low * 0.999;
            
            // 检查止损距离
            double riskDistance = e_close - stopLoss;
            if(atr > 0 && riskDistance > atr * InpMaxStopATRMult)
                return SIGNAL_NONE;
            
            baseHeight = atr * 2.0;
            
            UpdateSignalCooldown("buy");
            return SIGNAL_SPIKE_BUY;
        }
    }
    
    // 向下 Spike
    if(s_close < s_open && e_close < e_open)
    {
        double signalBodyRatio = s_body / s_range;
        double entryBodyRatio = e_body / e_range;
        
        if(signalBodyRatio > 0.65 && entryBodyRatio > 0.50 && s_low < min10Low && e_close < ema)
        {
            if(!CheckSignalCooldown("sell")) return SIGNAL_NONE;
            
            if(g_MarketState == MARKET_STATE_STRONG_TREND && g_TrendDirection == "up")
                return SIGNAL_NONE;
            
            stopLoss = s_high * 1.001;
            
            double riskDistance = stopLoss - e_close;
            if(atr > 0 && riskDistance > atr * InpMaxStopATRMult)
                return SIGNAL_NONE;
            
            baseHeight = atr * 2.0;
            
            UpdateSignalCooldown("sell");
            return SIGNAL_SPIKE_SELL;
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check H2/L2 Signal                                                |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckH2L2(double ema, double atr, double &stopLoss, double &baseHeight)
{
    double close = g_CloseBuffer[1];
    double high = g_HighBuffer[1];
    double low = g_LowBuffer[1];
    
    // 更新 H2 状态机
    ENUM_SIGNAL_TYPE h2Signal = UpdateH2StateMachine(close, high, low, ema, atr, stopLoss, baseHeight);
    if(h2Signal != SIGNAL_NONE)
        return h2Signal;
    
    // 更新 L2 状态机
    ENUM_SIGNAL_TYPE l2Signal = UpdateL2StateMachine(close, high, low, ema, atr, stopLoss, baseHeight);
    if(l2Signal != SIGNAL_NONE)
        return l2Signal;
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Update H2 State Machine                                           |
//| Al Brooks: 强趋势中放宽 Counting Bars 要求                         |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE UpdateH2StateMachine(double close, double high, double low, 
                                       double ema, double atr,
                                       double &stopLoss, double &baseHeight)
{
    ENUM_SIGNAL_TYPE signal = SIGNAL_NONE;
    double emaTolerance = ema * 0.003; // 0.3% 容差
    
    bool isInUptrend = close >= (ema - emaTolerance);
    
    //=================================================================
    // 【新增】StrongTrendOverride: 强趋势中放宽回调要求
    // 在 STRONG_TREND 或 TIGHT_CHANNEL 中，不强制要求反向 K 线
    //=================================================================
    bool isStrongTrendMode = (g_MarketState == MARKET_STATE_STRONG_TREND || 
                              g_MarketState == MARKET_STATE_TIGHT_CHANNEL);
    
    if(isInUptrend)
    {
        switch(g_H2State)
        {
            case H2_WAITING_FOR_PULLBACK:
                if(g_H2_TrendHigh == 0 || high > g_H2_TrendHigh)
                    g_H2_TrendHigh = high;
                break;
                
            case H2_IN_PULLBACK:
                if(g_H2_TrendHigh > 0 && high > g_H2_TrendHigh)
                {
                    g_H2State = H2_H1_DETECTED;
                    g_H2_H1High = high;
                    g_H2_H1BarIndex = 1;
                    
                    // 强趋势中触发 H1
                    if(g_H2_IsStrongTrend)
                    {
                        // 20 Gap Bar 法则检查：屏蔽第一次回测的 H1
                        if(Check20GapBarBlock("H1"))
                        {
                            // 被屏蔽，但继续状态机流转（等待 H2）
                            g_H2_IsStrongTrend = false;
                        }
                        else if(CheckSignalCooldown("buy"))
                        {
                            stopLoss = CalculateStopLoss("buy", atr);
                            if(stopLoss > 0)
                            {
                                baseHeight = atr * 2.0;
                                signal = SIGNAL_H1_BUY;
                                UpdateSignalCooldown("buy");
                            }
                        }
                        g_H2_IsStrongTrend = false;
                    }
                }
                break;
                
            case H2_H1_DETECTED:
                // 强趋势下：当前 K 线为 Inside Bar 时不重置（Al Brooks 时间回调）
                if(g_H2_PullbackStartLow > 0 && low < g_H2_PullbackStartLow)
                {
                    if(!(isStrongTrendMode && IsInsideBar(1)))
                    {
                        ResetH2StateMachine();
                        g_H2_TrendHigh = high;
                    }
                }
                else if(high > g_H2_H1High)
                {
                    g_H2_H1High = high;
                    g_H2_H1BarIndex = 1;
                }
                else if(g_H2_H1High > 0 && low < g_H2_H1High)
                {
                    g_H2State = H2_WAITING_FOR_H2;
                }
                break;
                
            case H2_WAITING_FOR_H2:
                // 入场触发：价格突破横盘区间最高点即触发 H2（Al Brooks 等距突破）
                if(g_H2_H1High > 0 && high > g_H2_H1High)
                {
                    //=================================================================
                    // 强趋势：Inside Bar / 小实体棒视为有效“时间回调”，突破即触发
                    // 正常：仍需 Counting Bars（价格或时间回调）
                    //=================================================================
                    bool validCountingBars = false;
                    
                    if(isStrongTrendMode)
                    {
                        // 停顿棒（Doji、Inside Bar、小实体）或 时间回调棒 任一即可
                        validCountingBars = HasPauseBars(g_H2_H1BarIndex, 1, atr) ||
                                            HasTimeCorrectionBars(g_H2_H1BarIndex, 1, atr);
                        
                        if(validCountingBars)
                        {
                            Print("📊 H2 时间回调确认: 横盘/Inside Bar 有效 → 突破 ", DoubleToString(g_H2_H1High, g_SymbolDigits), " 触发");
                        }
                    }
                    else
                    {
                        validCountingBars = HasCountingBars(g_H2_H1BarIndex, 1, true);
                    }
                    
                    if(validCountingBars)
                    {
                        if(CheckSignalCooldown("buy"))
                        {
                            stopLoss = CalculateStopLoss("buy", atr);
                            if(stopLoss > 0 && ValidateSignalBar("buy"))
                            {
                                baseHeight = atr * 2.0;
                                signal = SIGNAL_H2_BUY;
                                UpdateSignalCooldown("buy");
                            }
                        }
                    }
                    
                    ResetH2StateMachine();
                    g_H2_TrendHigh = high;
                }
                else if(g_H2_PullbackStartLow > 0 && low < g_H2_PullbackStartLow)
                {
                    // 强趋势下：Inside Bar 不重置
                    if(!(isStrongTrendMode && IsInsideBar(1)))
                    {
                        ResetH2StateMachine();
                        g_H2_TrendHigh = high;
                    }
                }
                break;
        }
    }
    else // 价格在 EMA 下方
    {
        switch(g_H2State)
        {
            case H2_WAITING_FOR_PULLBACK:
                if(close < (ema - emaTolerance))
                {
                    g_H2State = H2_IN_PULLBACK;
                    g_H2_PullbackStartLow = low;
                }
                break;
                
            case H2_IN_PULLBACK:
                if(g_H2_PullbackStartLow == 0 || low < g_H2_PullbackStartLow)
                    g_H2_PullbackStartLow = low;
                break;
                
            case H2_H1_DETECTED:
            case H2_WAITING_FOR_H2:
                if(g_H2_PullbackStartLow > 0 && low < g_H2_PullbackStartLow)
                {
                    if(!(isStrongTrendMode && IsInsideBar(1)))
                        ResetH2StateMachine();
                }
                break;
        }
    }
    
    return signal;
}

//+------------------------------------------------------------------+
//| Update L2 State Machine                                           |
//| Al Brooks: 强趋势中放宽 Counting Bars 要求                         |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE UpdateL2StateMachine(double close, double high, double low, 
                                       double ema, double atr,
                                       double &stopLoss, double &baseHeight)
{
    ENUM_SIGNAL_TYPE signal = SIGNAL_NONE;
    double emaTolerance = ema * 0.003;
    
    bool isInDowntrend = close <= (ema + emaTolerance);
    
    //=================================================================
    // 【新增】StrongTrendOverride: 强趋势中放宽回调要求
    //=================================================================
    bool isStrongTrendMode = (g_MarketState == MARKET_STATE_STRONG_TREND || 
                              g_MarketState == MARKET_STATE_TIGHT_CHANNEL);
    
    if(isInDowntrend)
    {
        switch(g_L2State)
        {
            case L2_WAITING_FOR_BOUNCE:
                if(g_L2_TrendLow == 0 || low < g_L2_TrendLow)
                    g_L2_TrendLow = low;
                break;
                
            case L2_IN_BOUNCE:
                if(g_L2_TrendLow > 0 && low < g_L2_TrendLow)
                {
                    g_L2State = L2_L1_DETECTED;
                    g_L2_L1Low = low;
                    g_L2_L1BarIndex = 1;
                    
                    if(g_L2_IsStrongTrend)
                    {
                        // 20 Gap Bar 法则检查：屏蔽第一次回测的 L1
                        if(Check20GapBarBlock("L1"))
                        {
                            // 被屏蔽，但继续状态机流转（等待 L2）
                            g_L2_IsStrongTrend = false;
                        }
                        else if(CheckSignalCooldown("sell"))
                        {
                            stopLoss = CalculateStopLoss("sell", atr);
                            if(stopLoss > 0)
                            {
                                baseHeight = atr * 2.0;
                                signal = SIGNAL_L1_SELL;
                                UpdateSignalCooldown("sell");
                            }
                        }
                        g_L2_IsStrongTrend = false;
                    }
                }
                break;
                
            case L2_L1_DETECTED:
                // 强趋势下：当前 K 线为 Inside Bar 时不重置（Al Brooks 时间回调）
                if(g_L2_BounceStartHigh > 0 && high > g_L2_BounceStartHigh)
                {
                    if(!(isStrongTrendMode && IsInsideBar(1)))
                    {
                        ResetL2StateMachine();
                        g_L2_TrendLow = low;
                    }
                }
                else if(low < g_L2_L1Low)
                {
                    g_L2_L1Low = low;
                    g_L2_L1BarIndex = 1;
                }
                else if(g_L2_L1Low > 0 && high > g_L2_L1Low)
                {
                    g_L2State = L2_WAITING_FOR_L2;
                }
                break;
                
            case L2_WAITING_FOR_L2:
                // 入场触发：价格突破横盘区间最低点即触发 L2（Al Brooks 等距突破）
                if(g_L2_L1Low > 0 && low < g_L2_L1Low)
                {
                    //=================================================================
                    // 强趋势：Inside Bar / 小实体棒视为有效“时间回调”，突破即触发
                    //=================================================================
                    bool validCountingBars = false;
                    
                    if(isStrongTrendMode)
                    {
                        validCountingBars = HasPauseBars(g_L2_L1BarIndex, 1, atr) ||
                                            HasTimeCorrectionBars(g_L2_L1BarIndex, 1, atr);
                        
                        if(validCountingBars)
                        {
                            Print("📊 L2 时间回调确认: 横盘/Inside Bar 有效 → 突破 ", DoubleToString(g_L2_L1Low, g_SymbolDigits), " 触发");
                        }
                    }
                    else
                    {
                        validCountingBars = HasCountingBars(g_L2_L1BarIndex, 1, false);
                    }
                    
                    if(validCountingBars)
                    {
                        if(CheckSignalCooldown("sell"))
                        {
                            stopLoss = CalculateStopLoss("sell", atr);
                            if(stopLoss > 0 && ValidateSignalBar("sell"))
                            {
                                baseHeight = atr * 2.0;
                                signal = SIGNAL_L2_SELL;
                                UpdateSignalCooldown("sell");
                            }
                        }
                    }
                    
                    ResetL2StateMachine();
                    g_L2_TrendLow = low;
                }
                else if(g_L2_BounceStartHigh > 0 && high > g_L2_BounceStartHigh)
                {
                    // 强趋势下：Inside Bar 不重置
                    if(!(isStrongTrendMode && IsInsideBar(1)))
                    {
                        ResetL2StateMachine();
                        g_L2_TrendLow = low;
                    }
                }
                break;
        }
    }
    else // 价格在 EMA 上方
    {
        switch(g_L2State)
        {
            case L2_WAITING_FOR_BOUNCE:
                if(close > (ema + emaTolerance))
                {
                    g_L2State = L2_IN_BOUNCE;
                    g_L2_BounceStartHigh = high;
                }
                break;
                
            case L2_IN_BOUNCE:
                if(g_L2_BounceStartHigh == 0 || high > g_L2_BounceStartHigh)
                    g_L2_BounceStartHigh = high;
                break;
                
            case L2_L1_DETECTED:
            case L2_WAITING_FOR_L2:
                if(g_L2_BounceStartHigh > 0 && high > g_L2_BounceStartHigh)
                {
                    if(!(isStrongTrendMode && IsInsideBar(1)))
                        ResetL2StateMachine();
                }
                break;
        }
    }
    
    return signal;
}

//+------------------------------------------------------------------+
//| Reset H2 State Machine                                            |
//+------------------------------------------------------------------+
void ResetH2StateMachine()
{
    g_H2State = H2_WAITING_FOR_PULLBACK;
    g_H2_TrendHigh = 0;
    g_H2_PullbackStartLow = 0;
    g_H2_H1High = 0;
    g_H2_H1BarIndex = -1;
    g_H2_IsStrongTrend = false;
}

//+------------------------------------------------------------------+
//| Reset L2 State Machine                                            |
//+------------------------------------------------------------------+
void ResetL2StateMachine()
{
    g_L2State = L2_WAITING_FOR_BOUNCE;
    g_L2_TrendLow = 0;
    g_L2_BounceStartHigh = 0;
    g_L2_L1Low = 0;
    g_L2_L1BarIndex = -1;
    g_L2_IsStrongTrend = false;
}

//+------------------------------------------------------------------+
//| Is Inside Bar (当前 K 线是否为内包线)                              |
//| 定义：高点不高于前高，低点不低于前低 (high <= prevHigh && low >= prevLow) |
//| barIndex: 1 = 当前棒，2 = 前一根棒                                 |
//+------------------------------------------------------------------+
bool IsInsideBar(int barIndex)
{
    if(barIndex < 1 || barIndex + 1 >= ArraySize(g_HighBuffer) || barIndex + 1 >= ArraySize(g_LowBuffer))
        return false;
    
    double currHigh = g_HighBuffer[barIndex];
    double currLow  = g_LowBuffer[barIndex];
    double prevHigh = g_HighBuffer[barIndex + 1];
    double prevLow  = g_LowBuffer[barIndex + 1];
    
    return (currHigh <= prevHigh && currLow >= prevLow);
}

//+------------------------------------------------------------------+
//| Is Small Body Bar (是否为小实体棒，用于时间回调判定)                |
//| 实体 < 0.35×ATR 或 实体 < 范围×20%                                 |
//+------------------------------------------------------------------+
bool IsSmallBodyBar(int barIndex, double atr)
{
    if(barIndex >= ArraySize(g_OpenBuffer) || barIndex >= ArraySize(g_CloseBuffer)) return false;
    if(barIndex >= ArraySize(g_HighBuffer) || barIndex >= ArraySize(g_LowBuffer)) return false;
    
    double open  = g_OpenBuffer[barIndex];
    double close = g_CloseBuffer[barIndex];
    double high  = g_HighBuffer[barIndex];
    double low   = g_LowBuffer[barIndex];
    
    double body  = MathAbs(close - open);
    double range = high - low;
    
    if(range <= 0) return false;
    
    if(atr > 0 && body < atr * 0.35) return true;
    if(body < range * 0.20) return true;  // Doji 型
    
    return false;
}

//+------------------------------------------------------------------+
//| Has Time Correction Bars (强趋势中：是否有有效的“时间回调”)          |
//| 连续或单根 Inside Bar / 小实体棒 均视为有效 Counting Bars            |
//+------------------------------------------------------------------+
bool HasTimeCorrectionBars(int startBar, int endBar, double atr)
{
    if(startBar < 0 || startBar <= endBar) return false;
    
    int count = 0;
    for(int i = endBar + 1; i < startBar; i++)
    {
        if(i >= ArraySize(g_HighBuffer)) break;
        
        if(IsInsideBar(i))
            count++;
        else if(IsSmallBodyBar(i, atr))
            count++;
        
        if(count >= 1) return true;  // 至少 1 根即视为有效时间回调
    }
    
    return count >= 1;
}

//+------------------------------------------------------------------+
//| Has Counting Bars (检查回调/反弹深度)                              |
//| Al Brooks: 回调有两种形式：                                         |
//|   1. 价格回调 (Correction in Price)：反向 K 线（阴线/阳线）          |
//|   2. 时间回调 (Correction in Time)：横盘整理（Inside Bar, Doji）    |
//| 时间回调代表趋势方非常强势，不允许价格回调，只通过时间消化超买/超卖    |
//+------------------------------------------------------------------+
bool HasCountingBars(int startBar, int endBar, bool lookForBearish)
{
    if(startBar < 0 || startBar <= endBar) return false;
    
    int priceCorrection = 0;   // 价格回调计数（反向 K 线）
    int timeCorrection = 0;    // 时间回调计数（横盘整理）
    int consecutiveSideways = 0; // 连续横盘棒计数
    
    for(int i = endBar + 1; i < startBar; i++)
    {
        if(i >= ArraySize(g_CloseBuffer)) break;
        if(i >= ArraySize(g_OpenBuffer)) break;
        if(i >= ArraySize(g_HighBuffer)) break;
        if(i >= ArraySize(g_LowBuffer)) break;
        
        double open = g_OpenBuffer[i];
        double close = g_CloseBuffer[i];
        double high = g_HighBuffer[i];
        double low = g_LowBuffer[i];
        double body = MathAbs(close - open);
        double range = high - low;
        
        //=============================================================
        // 检测价格回调（反向 K 线）
        //=============================================================
        if(lookForBearish)
        {
            // H2 买入信号需要阴线回调
            if(close < open) priceCorrection++;
        }
        else
        {
            // L2 卖出信号需要阳线回调
            if(close > open) timeCorrection++;
        }
        
        //=============================================================
        // 检测时间回调（横盘整理）
        // Al Brooks: 横盘是强势方的表现，不允许价格回调
        //=============================================================
        bool isSidewaysBar = false;
        
        // Doji：实体 < K 线范围的 15%
        if(range > 0 && body < range * 0.15)
        {
            isSidewaysBar = true;
        }
        
        // Inside Bar：完全包含在前一根 K 线内
        if(i + 1 < ArraySize(g_HighBuffer) && i + 1 < ArraySize(g_LowBuffer))
        {
            double prevHigh = g_HighBuffer[i + 1];
            double prevLow = g_LowBuffer[i + 1];
            if(high <= prevHigh && low >= prevLow)
            {
                isSidewaysBar = true;
            }
        }
        
        // 小实体棒：实体小于前一根实体的 50%
        if(i + 1 < ArraySize(g_OpenBuffer) && i + 1 < ArraySize(g_CloseBuffer))
        {
            double prevBody = MathAbs(g_CloseBuffer[i + 1] - g_OpenBuffer[i + 1]);
            if(prevBody > 0 && body < prevBody * 0.5)
            {
                isSidewaysBar = true;
            }
        }
        
        if(isSidewaysBar)
        {
            timeCorrection++;
            consecutiveSideways++;
        }
        else
        {
            consecutiveSideways = 0; // 重置连续计数
        }
    }
    
    //=================================================================
    // 判定逻辑
    //=================================================================
    
    // 情况 1：有价格回调（至少 1 根反向 K 线）
    if(priceCorrection >= 1)
    {
        return true;
    }
    
    // 情况 2：强趋势中的时间回调
    // 在 STRONG_TREND 或 TIGHT_CHANNEL 下，连续 2 根横盘棒也算有效回调
    bool isStrongTrend = (g_MarketState == MARKET_STATE_STRONG_TREND || 
                          g_MarketState == MARKET_STATE_TIGHT_CHANNEL ||
                          g_MarketState == MARKET_STATE_BREAKOUT);
    
    if(isStrongTrend && timeCorrection >= 2)
    {
        Print("📊 时间回调确认: ", timeCorrection, " 根横盘棒 (Inside Bar/Doji)");
        Print("   Al Brooks: 横盘整理 = 强势方不允许价格回调，只通过时间消化");
        return true;
    }
    
    // 情况 3：区间太短（1-2 根 K 线）
    int barCount = startBar - endBar - 1;
    if(barCount <= 1)
    {
        return true; // 区间太短，放宽要求
    }
    
    return false;
}

//+------------------------------------------------------------------+
//| Has Pause Bars (停顿棒检测 - 强趋势中的替代逻辑)                     |
//| Al Brooks: 强趋势中的回调可以很浅，只需有"犹豫"即可                   |
//| 停顿棒类型：                                                        |
//| - Doji：实体 < K 线范围的 10%                                       |
//| - Inside Bar：完全包含在前一根 K 线内                                |
//| - 小实体棒：实体 < 0.3 × ATR                                        |
//+------------------------------------------------------------------+
bool HasPauseBars(int startBar, int endBar, double atr)
{
    if(startBar < 0 || startBar <= endBar) return false;
    
    // 遍历 H1/L1 到当前信号棒之间的所有 K 线
    for(int i = endBar + 1; i < startBar; i++)
    {
        if(i >= ArraySize(g_CloseBuffer)) break;
        if(i >= ArraySize(g_HighBuffer)) break;
        if(i >= ArraySize(g_LowBuffer)) break;
        if(i >= ArraySize(g_OpenBuffer)) break;
        
        double open = g_OpenBuffer[i];
        double close = g_CloseBuffer[i];
        double high = g_HighBuffer[i];
        double low = g_LowBuffer[i];
        
        double body = MathAbs(close - open);
        double range = high - low;
        
        // 防止除零
        if(range <= 0) continue;
        
        //=================================================================
        // 检测 Doji：实体 < K 线范围的 10%
        //=================================================================
        bool isDoji = (body < range * 0.1);
        
        //=================================================================
        // 检测小实体棒：实体 < 0.3 × ATR
        //=================================================================
        bool isSmallBody = (atr > 0 && body < atr * 0.3);
        
        //=================================================================
        // 检测 Inside Bar：完全包含在前一根 K 线内
        //=================================================================
        bool isInsideBar = false;
        if(i + 1 < ArraySize(g_HighBuffer) && i + 1 < ArraySize(g_LowBuffer))
        {
            double prevHigh = g_HighBuffer[i + 1];
            double prevLow = g_LowBuffer[i + 1];
            isInsideBar = (high <= prevHigh && low >= prevLow);
        }
        
        // 只要有一根停顿棒，即视为有效
        if(isDoji || isSmallBody || isInsideBar)
        {
            return true;
        }
    }
    
    // 如果没有找到停顿棒，但区间只有 1-2 根 K 线，也放宽要求
    // 这是因为强趋势中，回调本来就很浅
    int barCount = startBar - endBar - 1;
    if(barCount <= 2)
    {
        return true; // 强趋势中，1-2 根 K 线的回调已足够
    }
    
    return false;
}

//+------------------------------------------------------------------+
//| Check Failed Breakout                                             |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckFailedBreakout(double ema, double atr, double &stopLoss, double &baseHeight)
{
    int lookback = 10;
    
    // 计算近期高低点
    double maxHigh = g_HighBuffer[2];
    double minLow = g_LowBuffer[2];
    for(int i = 2; i <= lookback + 1; i++)
    {
        if(g_HighBuffer[i] > maxHigh) maxHigh = g_HighBuffer[i];
        if(g_LowBuffer[i] < minLow) minLow = g_LowBuffer[i];
    }
    
    double currentHigh = g_HighBuffer[1];
    double currentLow = g_LowBuffer[1];
    double currentClose = g_CloseBuffer[1];
    double currentOpen = g_OpenBuffer[1];
    double klineRange = currentHigh - currentLow;
    
    if(klineRange <= 0) return SIGNAL_NONE;
    
    // 创新高后反转
    if(currentHigh > maxHigh)
    {
        if(currentClose < currentOpen) // 阴线
        {
            double closePosition = (currentHigh - currentClose) / klineRange;
            if(closePosition >= 0.60)
            {
                if(CheckSignalCooldown("sell"))
                {
                    stopLoss = CalculateStopLoss("sell", atr);
                    if(stopLoss > 0)
                    {
                        baseHeight = maxHigh - minLow;
                        UpdateSignalCooldown("sell");
                        return SIGNAL_FAILED_BO_SELL;
                    }
                }
            }
        }
    }
    
    // 创新低后反转
    if(currentLow < minLow)
    {
        if(currentClose > currentOpen) // 阳线
        {
            double closePosition = (currentClose - currentLow) / klineRange;
            if(closePosition >= 0.60)
            {
                if(CheckSignalCooldown("buy"))
                {
                    stopLoss = CalculateStopLoss("buy", atr);
                    if(stopLoss > 0)
                    {
                        baseHeight = maxHigh - minLow;
                        UpdateSignalCooldown("buy");
                        return SIGNAL_FAILED_BO_BUY;
                    }
                }
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Wedge Reversal                                              |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckWedge(double ema, double atr, double &stopLoss, double &baseHeight)
{
    int lookback = 30;
    
    // 查找三推高点递降（楔顶）
    double peaks[3] = {0, 0, 0};
    int peakIndices[3] = {-1, -1, -1};
    int peakCount = 0;
    
    for(int i = 3; i <= lookback && peakCount < 3; i++)
    {
        // 简单的峰值检测
        if(g_HighBuffer[i] > g_HighBuffer[i-1] && 
           g_HighBuffer[i] > g_HighBuffer[i-2] &&
           g_HighBuffer[i] > g_HighBuffer[i+1] && 
           g_HighBuffer[i] > g_HighBuffer[i+2])
        {
            if(peakCount == 0 || g_HighBuffer[i] < peaks[peakCount-1])
            {
                peaks[peakCount] = g_HighBuffer[i];
                peakIndices[peakCount] = i;
                peakCount++;
            }
        }
    }
    
    // 检测楔顶反转（三推高点递降后做空）
    if(peakCount >= 3)
    {
        double wedgeHigh = peaks[0];
        
        // 当前K线突破后回落
        if(g_HighBuffer[1] > wedgeHigh * 0.999)
        {
            double klineRange = g_HighBuffer[1] - g_LowBuffer[1];
            if(klineRange > 0)
            {
                double closePosition = (g_HighBuffer[1] - g_CloseBuffer[1]) / klineRange;
                if(closePosition >= 0.50 && g_CloseBuffer[1] < g_OpenBuffer[1])
                {
                    if(CheckSignalCooldown("sell"))
                    {
                        stopLoss = wedgeHigh + (atr > 0 ? 0.5 * atr : wedgeHigh * 0.001);
                        baseHeight = wedgeHigh - g_LowBuffer[peakIndices[2]];
                        UpdateSignalCooldown("sell");
                        return SIGNAL_WEDGE_SELL;
                    }
                }
            }
        }
    }
    
    // 查找三推低点递升（楔底）
    double troughs[3] = {0, 0, 0};
    int troughIndices[3] = {-1, -1, -1};
    int troughCount = 0;
    
    for(int i = 3; i <= lookback && troughCount < 3; i++)
    {
        if(g_LowBuffer[i] < g_LowBuffer[i-1] && 
           g_LowBuffer[i] < g_LowBuffer[i-2] &&
           g_LowBuffer[i] < g_LowBuffer[i+1] && 
           g_LowBuffer[i] < g_LowBuffer[i+2])
        {
            if(troughCount == 0 || g_LowBuffer[i] > troughs[troughCount-1])
            {
                troughs[troughCount] = g_LowBuffer[i];
                troughIndices[troughCount] = i;
                troughCount++;
            }
        }
    }
    
    // 检测楔底反转（三推低点递升后做多）
    if(troughCount >= 3)
    {
        double wedgeLow = troughs[0];
        
        if(g_LowBuffer[1] < wedgeLow * 1.001)
        {
            double klineRange = g_HighBuffer[1] - g_LowBuffer[1];
            if(klineRange > 0)
            {
                double closePosition = (g_CloseBuffer[1] - g_LowBuffer[1]) / klineRange;
                if(closePosition >= 0.50 && g_CloseBuffer[1] > g_OpenBuffer[1])
                {
                    if(CheckSignalCooldown("buy"))
                    {
                        stopLoss = wedgeLow - (atr > 0 ? 0.5 * atr : wedgeLow * 0.001);
                        baseHeight = g_HighBuffer[troughIndices[2]] - wedgeLow;
                        UpdateSignalCooldown("buy");
                        return SIGNAL_WEDGE_BUY;
                    }
                }
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Climax Reversal (高潮反转检测)                               |
//| Al Brooks PA 核心原则：                                             |
//| 1. Spike 阶段默认屏蔽逆势（保护新手，Spike 会持续得比想象中更久）     |
//| 2. V 型反转是高级信号，需要极高门槛才能在 Spike 中触发               |
//| strictMode = true: Spike V 型反转，要求极端行情 + 强力反转          |
//| strictMode = false: 正常 TradingRange/FinalFlag 调用               |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckClimax(double ema, double atr, double &stopLoss, double &baseHeight, 
                             bool strictMode = false)
{
    if(atr <= 0) return SIGNAL_NONE;
    
    //=================================================================
    // Spike V 型反转开关检查
    //=================================================================
    if(strictMode && !InpEnableSpikeClimax)
    {
        return SIGNAL_NONE;  // 禁用 Spike 中的反转
    }
    
    //=================================================================
    // 动态 Climax ATR 倍数
    // 正常模式：2.5 × ATR
    // Spike V 型反转：使用输入参数 InpSpikeClimaxATRMult
    //=================================================================
    double climaxMult = strictMode ? InpSpikeClimaxATRMult : 2.5;
    
    // 前一根K线（潜在的 Climax 棒）
    double prevHigh = g_HighBuffer[2];
    double prevLow = g_LowBuffer[2];
    double prevOpen = g_OpenBuffer[2];
    double prevClose = g_CloseBuffer[2];
    double prevRange = prevHigh - prevLow;
    double prevBody = MathAbs(prevClose - prevOpen);
    
    // 当前K线（潜在的反转棒）
    double currHigh = g_HighBuffer[1];
    double currLow = g_LowBuffer[1];
    double currOpen = g_OpenBuffer[1];
    double currClose = g_CloseBuffer[1];
    double currRange = currHigh - currLow;
    double currBody = MathAbs(currClose - currOpen);
    
    if(currRange <= 0 || prevBody <= 0) return SIGNAL_NONE;
    
    //=================================================================
    // 【Spike V 型反转】多重门槛检查
    //=================================================================
    if(strictMode)
    {
        //=============================================================
        // 门槛 1: Spike 持续时间检查
        // V 型反转通常发生在 Spike 末期（连续多根趋势棒之后）
        //=============================================================
        int spikeBars = CountConsecutiveTrendBars();
        if(spikeBars < InpMinSpikeBars)
        {
            return SIGNAL_NONE;  // Spike 尚未成熟，不允许反转
        }
        
        //=============================================================
        // 门槛 2: Climax 棒长度检查（必须是极端长棒）
        //=============================================================
        if(prevRange < atr * climaxMult)
        {
            return SIGNAL_NONE;  // 不够极端
        }
        
        //=============================================================
        // 门槛 3: 反转棒覆盖率（必须有足够的实体）
        //=============================================================
        double reversalCoverage = currBody / prevBody;
        if(reversalCoverage < InpReversalCoverage)
        {
            return SIGNAL_NONE;
        }
        
        //=============================================================
        // 门槛 4: 反转棒穿透率（必须穿入 Climax 棒实体）
        // 做空反转：反转棒必须穿入 Climax 阳线实体的一定比例
        // 做多反转：反转棒必须穿入 Climax 阴线实体的一定比例
        //=============================================================
        double penetration = 0;
        bool isBullishClimax = (prevClose > prevOpen);  // Climax 是阳线
        bool isBearishClimax = (prevClose < prevOpen);  // Climax 是阴线
        
        if(isBullishClimax)
        {
            // 向上 Climax -> 反转棒应穿入 Climax 实体下方
            double climaxBodyLow = prevOpen;   // Climax 阳线的实体底部
            double climaxBodyHigh = prevClose; // Climax 阳线的实体顶部
            double climaxBodySize = climaxBodyHigh - climaxBodyLow;
            
            if(climaxBodySize > 0)
            {
                // 反转棒收盘应该穿透 Climax 实体
                double penetrationDepth = climaxBodyHigh - currClose;
                penetration = penetrationDepth / climaxBodySize;
            }
        }
        else if(isBearishClimax)
        {
            // 向下 Climax -> 反转棒应穿入 Climax 实体上方
            double climaxBodyLow = prevClose;  // Climax 阴线的实体底部
            double climaxBodyHigh = prevOpen;  // Climax 阴线的实体顶部
            double climaxBodySize = climaxBodyHigh - climaxBodyLow;
            
            if(climaxBodySize > 0)
            {
                double penetrationDepth = currClose - climaxBodyLow;
                penetration = penetrationDepth / climaxBodySize;
            }
        }
        
        if(penetration < InpReversalPenetration)
        {
            return SIGNAL_NONE;  // 穿透不够深
        }
        
        //=============================================================
        // 门槛 5: 反转棒收盘位置（必须在强势区域）
        // 做空反转：收盘应在下半部（弱势端）
        // 做多反转：收盘应在上半部（强势端）
        //=============================================================
        double closePosition = (currClose - currLow) / currRange;
        
        if(isBullishClimax)  // 做空反转
        {
            // 收盘应该在下方（弱势端）
            if(closePosition > (1.0 - InpReversalClosePos))
            {
                return SIGNAL_NONE;
            }
        }
        else if(isBearishClimax)  // 做多反转
        {
            // 收盘应该在上方（强势端）
            if(closePosition < InpReversalClosePos)
            {
                return SIGNAL_NONE;
            }
        }
        
        //=============================================================
        // 门槛 6: 第二入场检查 (Al Brooks: 强趋势第一次反转80%失败)
        // 只有当启用且有之前失败的反转尝试时，才允许触发信号
        //=============================================================
        if(InpRequireSecondEntry)
        {
            string attemptDirection = isBullishClimax ? "bearish" : "bullish";
            
            // 检查是否有之前的失败反转尝试
            bool hasFailedAttempt = CheckForFailedReversalAttempt(attemptDirection, atr);
            
            if(!hasFailedAttempt)
            {
                // 记录当前为"第一次反转尝试"，但不发出信号
                RecordReversalAttempt(attemptDirection, isBullishClimax ? currLow : currHigh);
                
                Print("━━━━━━━━ V 型反转: 第一次尝试 (不触发) ━━━━━━━━");
                Print("   Al Brooks: 强趋势中第一次反转尝试80%会失败");
                Print("   方向: ", attemptDirection);
                Print("   极值: ", DoubleToString(isBullishClimax ? currLow : currHigh, g_SymbolDigits));
                Print("   等待: 价格突破此极值后的第二次反转尝试");
                
                return SIGNAL_NONE;  // 不发出信号，等待第二入场
            }
            else
            {
                Print("━━━━━━━━ V 型反转: 第二入场确认 ━━━━━━━━");
                Print("   Al Brooks: 第一次反转已失败，第二次入场成功率更高");
                // 继续执行，发出信号
            }
        }
        
        //=============================================================
        // 所有门槛通过 - 记录日志
        //=============================================================
        Print("━━━━━━━━ V 型反转检测通过 ━━━━━━━━");
        Print("   Spike 持续: ", spikeBars, " 根 K 线");
        Print("   Climax 棒长度: ", DoubleToString(prevRange / atr, 2), "×ATR (阈值: ", InpSpikeClimaxATRMult, ")");
        Print("   反转棒覆盖率: ", DoubleToString(reversalCoverage * 100, 1), "% (阈值: ", InpReversalCoverage * 100, "%)");
        Print("   反转棒穿透率: ", DoubleToString(penetration * 100, 1), "% (阈值: ", InpReversalPenetration * 100, "%)");
        Print("   收盘位置: ", DoubleToString(closePosition * 100, 1), "% (阈值: ", InpReversalClosePos * 100, "%)");
    }
    
    //=================================================================
    // 向上 Climax -> 做空反转
    //=================================================================
    if(prevRange > atr * climaxMult && prevClose > prevOpen)
    {
        // 当前棒必须是阴线，且收盘低于前一根收盘
        if(currClose < currOpen && currClose < prevClose)
        {
            // 尾部影线检查（上影线表示空头力量）
            double upperTail = currHigh - MathMax(currOpen, currClose);
            double tailRatio = upperTail / currRange;
            
            double minTailRatio = strictMode ? 0.20 : 0.15;
            
            if(tailRatio >= minTailRatio)
            {
                if(CheckSignalCooldown("sell"))
                {
                    // 检查前期走势（必须有足够的上涨空间）
                    double lookbackLow = g_LowBuffer[3];
                    for(int i = 3; i <= 10; i++)
                        if(g_LowBuffer[i] < lookbackLow) lookbackLow = g_LowBuffer[i];
                    
                    double priorMove = prevHigh - lookbackLow;
                    double minPriorMove = strictMode ? atr * 4.0 : atr * 2.0;
                    
                    if(priorMove >= minPriorMove)
                    {
                        stopLoss = CalculateStopLoss("sell", atr);
                        if(stopLoss > 0)
                        {
                            baseHeight = prevRange;
                            UpdateSignalCooldown("sell");
                            
                            if(strictMode)
                            {
                                Print("🔴 V 型反转 SELL 触发!");
                                Print("   前期上涨: ", DoubleToString(priorMove / atr, 1), "×ATR");
                                Print("   ⚠️ Al Brooks: 高级信号，需严格风控");
                            }
                            return SIGNAL_CLIMAX_SELL;
                        }
                    }
                }
            }
        }
    }
    
    //=================================================================
    // 向下 Climax -> 做多反转
    //=================================================================
    if(prevRange > atr * climaxMult && prevClose < prevOpen)
    {
        // 当前棒必须是阳线，且收盘高于前一根收盘
        if(currClose > currOpen && currClose > prevClose)
        {
            // 尾部影线检查（下影线表示多头力量）
            double lowerTail = MathMin(currOpen, currClose) - currLow;
            double tailRatio = lowerTail / currRange;
            
            double minTailRatio = strictMode ? 0.20 : 0.15;
            
            if(tailRatio >= minTailRatio)
            {
                if(CheckSignalCooldown("buy"))
                {
                    double lookbackHigh = g_HighBuffer[3];
                    for(int i = 3; i <= 10; i++)
                        if(g_HighBuffer[i] > lookbackHigh) lookbackHigh = g_HighBuffer[i];
                    
                    double priorMove = lookbackHigh - prevLow;
                    double minPriorMove = strictMode ? atr * 4.0 : atr * 2.0;
                    
                    if(priorMove >= minPriorMove)
                    {
                        stopLoss = CalculateStopLoss("buy", atr);
                        if(stopLoss > 0)
                        {
                            baseHeight = prevRange;
                            UpdateSignalCooldown("buy");
                            
                            if(strictMode)
                            {
                                Print("🟢 V 型反转 BUY 触发!");
                                Print("   前期下跌: ", DoubleToString(priorMove / atr, 1), "×ATR");
                                Print("   ⚠️ Al Brooks: 高级信号，需严格风控");
                            }
                            return SIGNAL_CLIMAX_BUY;
                        }
                    }
                }
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Count Consecutive Trend Bars (计算连续趋势棒数量)                   |
//| 用于判断 Spike 是否成熟到可以触发 V 型反转                          |
//+------------------------------------------------------------------+
int CountConsecutiveTrendBars()
{
    int count = 0;
    bool bullTrend = (g_CloseBuffer[2] > g_OpenBuffer[2]);  // 当前 Climax 方向
    
    // 从 bar[2] 开始向回数
    for(int i = 2; i <= 20; i++)
    {
        double barOpen = g_OpenBuffer[i];
        double barClose = g_CloseBuffer[i];
        double barRange = g_HighBuffer[i] - g_LowBuffer[i];
        double barBody = MathAbs(barClose - barOpen);
        
        if(barRange <= 0) break;
        
        // 检查是否是趋势方向的棒线
        bool isTrendBar = false;
        double bodyRatio = barBody / barRange;
        
        if(bullTrend)
        {
            // 上涨 Spike: 阳线或高收盘阴线（停顿棒）
            isTrendBar = (barClose > barOpen) || 
                         (barClose < barOpen && (barClose - g_LowBuffer[i]) / barRange > 0.6);
        }
        else
        {
            // 下跌 Spike: 阴线或低收盘阳线（停顿棒）
            isTrendBar = (barClose < barOpen) ||
                         (barClose > barOpen && (g_HighBuffer[i] - barClose) / barRange > 0.6);
        }
        
        // 趋势棒必须有一定的实体
        if(isTrendBar && bodyRatio >= 0.3)
        {
            count++;
        }
        else
        {
            break;  // 遇到非趋势棒，停止计数
        }
    }
    
    return count;
}

//+------------------------------------------------------------------+
//| Record Reversal Attempt (记录反转尝试)                              |
//| Al Brooks: 强趋势中第一次反转尝试 80% 会失败，记录以等待第二入场      |
//+------------------------------------------------------------------+
void RecordReversalAttempt(string direction, double extremePrice)
{
    g_LastReversalAttempt.time = TimeCurrent();
    g_LastReversalAttempt.price = extremePrice;
    g_LastReversalAttempt.direction = direction;
    g_LastReversalAttempt.failed = false;
    
    g_HasPendingReversal = true;
    g_ReversalAttemptCount++;
    
    Print("📝 反转尝试记录: 方向=", direction, 
          " | 极值=", DoubleToString(extremePrice, g_SymbolDigits),
          " | 尝试次数=", g_ReversalAttemptCount);
}

//+------------------------------------------------------------------+
//| Check For Failed Reversal Attempt (检查是否有失败的反转尝试)         |
//| 条件: 之前有反转尝试，且价格已突破了该尝试的极值（表示失败）           |
//+------------------------------------------------------------------+
bool CheckForFailedReversalAttempt(string direction, double atr)
{
    // 没有待处理的反转尝试
    if(!g_HasPendingReversal)
    {
        return false;
    }
    
    // 方向不匹配（例如之前是做多反转尝试，现在是做空反转尝试）
    if(g_LastReversalAttempt.direction != direction)
    {
        // 清除之前的记录，重新开始
        ClearReversalAttempt();
        return false;
    }
    
    // 检查时间窗口（超过 InpSecondEntryLookback 根 K 线后失效）
    datetime currentBarTime = iTime(_Symbol, PERIOD_CURRENT, 1);
    datetime attemptTime = g_LastReversalAttempt.time;
    
    // 粗略检查：如果时间差太大，清除记录
    int periodSeconds = PeriodSeconds(PERIOD_CURRENT);
    int maxTimeDiff = periodSeconds * InpSecondEntryLookback;
    
    if(currentBarTime - attemptTime > maxTimeDiff)
    {
        Print("⏰ 反转尝试过期: 超过 ", InpSecondEntryLookback, " 根 K 线");
        ClearReversalAttempt();
        return false;
    }
    
    // 检查是否已经失败（价格突破了反转尝试的极值）
    double extremePrice = g_LastReversalAttempt.price;
    double currentHigh = g_HighBuffer[1];
    double currentLow = g_LowBuffer[1];
    
    bool failed = false;
    
    if(direction == "bearish")
    {
        // 做空反转尝试：如果价格创了新高，则第一次尝试失败
        // 检查最近 N 根 K 线是否有突破
        for(int i = 1; i < InpSecondEntryLookback && i < ArraySize(g_HighBuffer); i++)
        {
            if(g_HighBuffer[i] > extremePrice + atr * 0.1)
            {
                failed = true;
                break;
            }
        }
    }
    else if(direction == "bullish")
    {
        // 做多反转尝试：如果价格创了新低，则第一次尝试失败
        for(int i = 1; i < InpSecondEntryLookback && i < ArraySize(g_LowBuffer); i++)
        {
            if(g_LowBuffer[i] < extremePrice - atr * 0.1)
            {
                failed = true;
                break;
            }
        }
    }
    
    if(failed && !g_LastReversalAttempt.failed)
    {
        g_LastReversalAttempt.failed = true;
        Print("❌ 第一次反转尝试失败: 价格突破了极值 ", DoubleToString(extremePrice, g_SymbolDigits));
        Print("   Al Brooks: 现在可以等待第二入场");
    }
    
    return g_LastReversalAttempt.failed;
}

//+------------------------------------------------------------------+
//| Clear Reversal Attempt (清除反转尝试记录)                           |
//+------------------------------------------------------------------+
void ClearReversalAttempt()
{
    g_HasPendingReversal = false;
    g_ReversalAttemptCount = 0;
    g_LastReversalAttempt.time = 0;
    g_LastReversalAttempt.price = 0;
    g_LastReversalAttempt.direction = "";
    g_LastReversalAttempt.failed = false;
}

//+------------------------------------------------------------------+
//| Update Reversal Attempt Tracking (更新反转尝试跟踪)                 |
//| 在每根新 K 线时调用，检查市场状态变化                                 |
//+------------------------------------------------------------------+
void UpdateReversalAttemptTracking()
{
    // 如果市场状态不再是强趋势，清除反转尝试记录
    bool isStrongTrend = (g_MarketState == MARKET_STATE_STRONG_TREND ||
                          g_MarketState == MARKET_STATE_BREAKOUT ||
                          g_MarketCycle == MARKET_CYCLE_SPIKE);
    
    if(!isStrongTrend && g_HasPendingReversal)
    {
        Print("📊 市场状态变化: 不再是强趋势，清除反转尝试记录");
        ClearReversalAttempt();
    }
}

//+------------------------------------------------------------------+
//| Check MTR (Major Trend Reversal)                                  |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckMTR(double ema, double atr, double &stopLoss, double &baseHeight)
{
    // 简化的 MTR 检测
    // 完整版需要趋势线突破 + 回测 + 反转信号棒
    
    int lookback = 60;
    
    // 识别趋势方向
    string trendDir = "";
    double extremePrice = 0;
    
    // 检查是否有显著趋势
    int higherHighs = 0;
    int lowerLows = 0;
    
    for(int i = 2; i <= lookback; i++)
    {
        if(i > lookback) break;
        if(g_HighBuffer[i] > g_HighBuffer[i+1]) higherHighs++;
        if(g_LowBuffer[i] < g_LowBuffer[i+1]) lowerLows++;
    }
    
    // 上升趋势
    if(higherHighs > lowerLows * 1.5 && higherHighs >= lookback * 0.4)
    {
        trendDir = "up";
        extremePrice = g_HighBuffer[2];
        for(int i = 2; i <= 10; i++)
            if(g_HighBuffer[i] > extremePrice) extremePrice = g_HighBuffer[i];
    }
    // 下降趋势
    else if(lowerLows > higherHighs * 1.5 && lowerLows >= lookback * 0.4)
    {
        trendDir = "down";
        extremePrice = g_LowBuffer[2];
        for(int i = 2; i <= 10; i++)
            if(g_LowBuffer[i] < extremePrice) extremePrice = g_LowBuffer[i];
    }
    
    if(trendDir == "") return SIGNAL_NONE;
    
    // 检查回测和反转
    double currClose = g_CloseBuffer[1];
    double currOpen = g_OpenBuffer[1];
    double currHigh = g_HighBuffer[1];
    double currLow = g_LowBuffer[1];
    
    if(trendDir == "up")
    {
        // 回测前高
        double tolerance = atr > 0 ? atr * 0.5 : extremePrice * 0.005;
        if(currHigh >= extremePrice - tolerance)
        {
            // 反转信号棒（阴线）
            if(currClose < currOpen && ValidateSignalBar("sell"))
            {
                if(CheckSignalCooldown("sell"))
                {
                    stopLoss = extremePrice + (atr > 0 ? atr * 0.5 : extremePrice * 0.005);
                    baseHeight = extremePrice - currClose;
                    if(baseHeight < atr * 0.5 && atr > 0) baseHeight = atr * 2.0;
                    UpdateSignalCooldown("sell");
                    return SIGNAL_MTR_SELL;
                }
            }
        }
    }
    else if(trendDir == "down")
    {
        // 回测前低
        double tolerance = atr > 0 ? atr * 0.5 : extremePrice * 0.005;
        if(currLow <= extremePrice + tolerance)
        {
            // 反转信号棒（阳线）
            if(currClose > currOpen && ValidateSignalBar("buy"))
            {
                if(CheckSignalCooldown("buy"))
                {
                    stopLoss = extremePrice - (atr > 0 ? atr * 0.5 : extremePrice * 0.005);
                    baseHeight = currClose - extremePrice;
                    if(baseHeight < atr * 0.5 && atr > 0) baseHeight = atr * 2.0;
                    UpdateSignalCooldown("buy");
                    return SIGNAL_MTR_BUY;
                }
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Final Flag                                                  |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckFinalFlag(double ema, double atr, double &stopLoss, double &baseHeight)
{
    double currClose = g_CloseBuffer[1];
    double currOpen = g_OpenBuffer[1];
    double currHigh = g_HighBuffer[1];
    double currLow = g_LowBuffer[1];
    double klineRange = currHigh - currLow;
    
    if(klineRange <= 0) return SIGNAL_NONE;
    
    // 根据之前的趋势方向决定反转方向
    if(g_TightChannelDir == "up")
    {
        // 之前上涨，现在寻找做空信号
        if(currClose < currOpen) // 阴线
        {
            double closePosition = (currHigh - currClose) / klineRange;
            if(closePosition >= 0.60 && ValidateSignalBar("sell"))
            {
                if(CheckSignalCooldown("sell"))
                {
                    stopLoss = g_TightChannelExtreme > 0 ? 
                               g_TightChannelExtreme + (atr > 0 ? atr * 0.5 : g_TightChannelExtreme * 0.005) :
                               currHigh * 1.005;
                    baseHeight = atr > 0 ? atr * 2.5 : klineRange * 2;
                    UpdateSignalCooldown("sell");
                    return SIGNAL_FINAL_FLAG_SELL;
                }
            }
        }
    }
    else if(g_TightChannelDir == "down")
    {
        // 之前下跌，现在寻找做多信号
        if(currClose > currOpen) // 阳线
        {
            double closePosition = (currClose - currLow) / klineRange;
            if(closePosition >= 0.60 && ValidateSignalBar("buy"))
            {
                if(CheckSignalCooldown("buy"))
                {
                    stopLoss = g_TightChannelExtreme > 0 ?
                               g_TightChannelExtreme - (atr > 0 ? atr * 0.5 : g_TightChannelExtreme * 0.005) :
                               currLow * 0.995;
                    baseHeight = atr > 0 ? atr * 2.5 : klineRange * 2;
                    UpdateSignalCooldown("buy");
                    return SIGNAL_FINAL_FLAG_BUY;
                }
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Validate Signal Bar (信号棒质量验证)                               |
//+------------------------------------------------------------------+
bool ValidateSignalBar(string side)
{
    double high = g_HighBuffer[1];
    double low = g_LowBuffer[1];
    double open = g_OpenBuffer[1];
    double close = g_CloseBuffer[1];
    
    double klineRange = high - low;
    if(klineRange <= 0) return false;
    
    double bodySize = MathAbs(close - open);
    double bodyRatio = bodySize / klineRange;
    
    // 实体占比检查
    if(bodyRatio < InpMinBodyRatio) return false;
    
    // 方向检查
    bool isBullish = close > open;
    bool isBearish = close < open;
    
    if(side == "buy" && !isBullish) return false;
    if(side == "sell" && !isBearish) return false;
    
    // 收盘位置检查
    if(side == "buy")
    {
        double closeFromHigh = (high - close) / klineRange;
        if(closeFromHigh > InpClosePositionPct) return false;
    }
    else
    {
        double closeFromLow = (close - low) / klineRange;
        if(closeFromLow > InpClosePositionPct) return false;
    }
    
    return true;
}

//+------------------------------------------------------------------+
//| Calculate Unified Stop Loss (统一止损计算)                         |
//| Al Brooks PA 核心原则：                                             |
//| 1. 强趋势 → Signal Bar 止损（逻辑性止损，收紧风险）                  |
//| 2. 震荡/通道 → Swing N=5 止损（结构性止损，容错空间大）               |
//| 硬性约束：止损距离不得超过 3×ATR                                     |
//+------------------------------------------------------------------+
double CalculateUnifiedStopLoss(string side, double atr, double entryPrice)
{
    // 前两根 K 线数据
    double signalHigh = g_HighBuffer[2];    // Signal Bar (bar[2])
    double signalLow = g_LowBuffer[2];
    double signalOpen = g_OpenBuffer[2];
    double signalClose = g_CloseBuffer[2];
    double entryHigh = g_HighBuffer[1];     // Entry Bar (bar[1])
    double entryLow = g_LowBuffer[1];
    
    //=================================================================
    // 获取当前实时点差（以价格为单位）
    //=================================================================
    double spreadPrice = GetCurrentSpreadPrice();
    
    //=================================================================
    // Buffer 计算（包含点差）
    // 强趋势用较小 Buffer，震荡用较大 Buffer
    //=================================================================
    bool isStrongTrend = (g_MarketState == MARKET_STATE_STRONG_TREND ||
                          g_MarketState == MARKET_STATE_BREAKOUT ||
                          g_MarketState == MARKET_STATE_TIGHT_CHANNEL);
    
    double atrBuffer = atr > 0 ? (isStrongTrend ? atr * 0.3 : atr * 0.5) : 0;
    double minBuffer = entryPrice * 0.002;  // 最小 0.2%
    double baseBuffer = MathMax(atrBuffer, minBuffer);
    double totalBuffer = baseBuffer + spreadPrice;
    
    //=================================================================
    // 根据市场状态选择止损策略
    //=================================================================
    double stopLoss = 0;
    double stopDistance = 0;
    string stopType = "";
    
    if(isStrongTrend)
    {
        //=============================================================
        // 【强趋势模式】Signal Bar 止损（逻辑性止损）
        // Al Brooks: 如果入场原因是那根强力趋势棒，价格就不应该跌破它
        // 收紧止损 → 更好的盈亏比
        // 不使用 Swing N=X，因为在强趋势中波段止损太宽，盈亏比差
        //=============================================================
        if(side == "buy")
        {
            // 做多：止损在 Signal Bar 低点下方
            // Signal Bar 是触发入场的那根棒线，价格不应跌破它的起始点
            stopLoss = signalLow - totalBuffer;
            stopDistance = entryPrice - stopLoss;
            stopType = "Signal Bar 止损 (逻辑性)";
            
            Print("📍 强趋势 BUY: ", stopType);
            Print("   MarketState: ", EnumToString(g_MarketState), " → 跳过 Swing, 使用 Signal Bar");
            Print("   Signal Bar Low = ", DoubleToString(signalLow, g_SymbolDigits));
            Print("   止损 = ", DoubleToString(stopLoss, g_SymbolDigits),
                  " | 距离 = ", DoubleToString(stopDistance / atr, 2), "×ATR");
            Print("   Al Brooks: 价格跌破 Signal Bar = 强趋势假设失效");
        }
        else
        {
            // 做空：止损在 Signal Bar 高点上方
            stopLoss = signalHigh + totalBuffer;
            stopDistance = stopLoss - entryPrice;
            stopType = "Signal Bar 止损 (逻辑性)";
            
            Print("📍 强趋势 SELL: ", stopType);
            Print("   MarketState: ", EnumToString(g_MarketState), " → 跳过 Swing, 使用 Signal Bar");
            Print("   Signal Bar High = ", DoubleToString(signalHigh, g_SymbolDigits));
            Print("   止损 = ", DoubleToString(stopLoss, g_SymbolDigits),
                  " | 距离 = ", DoubleToString(stopDistance / atr, 2), "×ATR");
            Print("   Al Brooks: 价格突破 Signal Bar = 强趋势假设失效");
        }
    }
    else
    {
        //=============================================================
        // 【震荡/通道模式】Swing 止损（结构性止损）
        // Al Brooks: 震荡市需要更大容错空间，防止被噪音打掉
        // N 值根据 g_MarketState 动态切换：
        //   TRADING_RANGE: N=5（宽幅震荡，大容错）
        //   CHANNEL: N=3（平衡）
        //   其他: N=3
        //=============================================================
        int swingLookback = 10;
        bool foundSwing = false;
        int swingDepth = GetSwingDepth();  // 获取动态 N 值
        
        if(side == "buy")
        {
            double swingLow = FindSwingLow(swingLookback);
            
            if(swingLow > 0)
            {
                stopLoss = swingLow - totalBuffer;
                stopDistance = entryPrice - stopLoss;
                
                // 检查是否在有效范围内
                if(atr > 0 && stopDistance <= atr * InpMaxStopATRMult && stopDistance > 0)
                {
                    foundSwing = true;
                    stopType = "Swing Low 止损 (结构性, N=" + IntegerToString(swingDepth) + ")";
                    
                    Print("📍 震荡/通道 BUY: ", stopType);
                    Print("   MarketState: ", EnumToString(g_MarketState), " → Swing Depth N=", swingDepth);
                    Print("   Swing Low = ", DoubleToString(swingLow, g_SymbolDigits));
                    Print("   止损 = ", DoubleToString(stopLoss, g_SymbolDigits),
                          " | 距离 = ", DoubleToString(stopDistance / atr, 2), "×ATR");
                    Print("   Al Brooks: 结构性止损防守整个波段");
                }
            }
            
            // 兜底：Swing 无效时用前两根 K 线极值
            if(!foundSwing)
            {
                double lowestLow = MathMin(signalLow, entryLow);
                stopLoss = lowestLow - totalBuffer;
                stopDistance = entryPrice - stopLoss;
                stopType = "前两根极值止损 (兜底)";
                
                Print("📐 震荡/通道 BUY: ", stopType);
                Print("   MarketState: ", EnumToString(g_MarketState), " → Swing N=", swingDepth, " 无效");
                Print("   最低点 = ", DoubleToString(lowestLow, g_SymbolDigits),
                      " | 距离 = ", DoubleToString(stopDistance / atr, 2), "×ATR");
            }
        }
        else
        {
            double swingHigh = FindSwingHigh(swingLookback);
            
            if(swingHigh > 0)
            {
                stopLoss = swingHigh + totalBuffer;
                stopDistance = stopLoss - entryPrice;
                
                if(atr > 0 && stopDistance <= atr * InpMaxStopATRMult && stopDistance > 0)
                {
                    foundSwing = true;
                    stopType = "Swing High 止损 (结构性, N=" + IntegerToString(swingDepth) + ")";
                    
                    Print("📍 震荡/通道 SELL: ", stopType);
                    Print("   MarketState: ", EnumToString(g_MarketState), " → Swing Depth N=", swingDepth);
                    Print("   Swing High = ", DoubleToString(swingHigh, g_SymbolDigits));
                    Print("   止损 = ", DoubleToString(stopLoss, g_SymbolDigits),
                          " | 距离 = ", DoubleToString(stopDistance / atr, 2), "×ATR");
                    Print("   Al Brooks: 结构性止损防守整个波段");
                }
            }
            
            if(!foundSwing)
            {
                double highestHigh = MathMax(signalHigh, entryHigh);
                stopLoss = highestHigh + totalBuffer;
                stopDistance = stopLoss - entryPrice;
                stopType = "前两根极值止损 (兜底)";
                
                Print("📐 震荡/通道 SELL: ", stopType);
                Print("   MarketState: ", EnumToString(g_MarketState), " → Swing N=", swingDepth, " 无效");
                Print("   最高点 = ", DoubleToString(highestHigh, g_SymbolDigits),
                      " | 距离 = ", DoubleToString(stopDistance / atr, 2), "×ATR");
            }
        }
    }
    
    //=================================================================
    // 硬性约束：止损距离不得超过 3×ATR
    //=================================================================
    if(atr > 0 && stopDistance > atr * InpMaxStopATRMult)
    {
        Print("⚠️ 止损距离 ", DoubleToString(stopDistance, g_SymbolDigits), 
              " 超过 ", InpMaxStopATRMult, "×ATR (", DoubleToString(atr * InpMaxStopATRMult, g_SymbolDigits), 
              ") - 信号被拒绝");
        Print("   详情: ATR=", DoubleToString(atr, g_SymbolDigits), 
              " | 点差=", DoubleToString(spreadPrice, g_SymbolDigits),
              " | 止损类型=", stopType);
        return 0; // 风险过大，返回 0 表示无效
    }
    
    //=================================================================
    // 使用品种正确的小数位数规范化价格
    //=================================================================
    stopLoss = NormalizeDouble(stopLoss, g_SymbolDigits);
    
    return stopLoss;
}

//+------------------------------------------------------------------+
//| Get Swing Depth (根据市场状态获取动态探测深度 N)                      |
//| STRONG_TREND / BREAKOUT: N=2（快速反应）                            |
//| TRADING_RANGE: N=5（更稳定的支撑阻力）                               |
//| 其他状态: N=3（平衡）                                                |
//+------------------------------------------------------------------+
int GetSwingDepth()
{
    switch(g_MarketState)
    {
        case MARKET_STATE_STRONG_TREND:
        case MARKET_STATE_BREAKOUT:
            return 2;  // 强趋势中需要快速反应
            
        case MARKET_STATE_TRADING_RANGE:
            return 5;  // 震荡区间需要更明显的 Swing 点
            
        case MARKET_STATE_CHANNEL:
        case MARKET_STATE_TIGHT_CHANNEL:
        case MARKET_STATE_FINAL_FLAG:
        default:
            return 3;  // 平衡状态
    }
}

//+------------------------------------------------------------------+
//| Check Bull Confirmation (检查多头确认 - Swing Low 右侧)             |
//| 条件 B：右侧至少有一根棒线的收盘价高于前一根棒线的高点                  |
//| 表示多头不仅是插针，而且夺回了控制权                                   |
//+------------------------------------------------------------------+
bool CheckBullConfirmation(int swingBarIndex)
{
    // 从 Swing Low 右侧（更近的棒）开始检查
    // swingBarIndex 是 Swing Low 所在的 bar index
    // 我们检查 bar[swingBarIndex-1] 到 bar[1] 是否有多头确认
    
    for(int i = swingBarIndex - 1; i >= 1; i--)
    {
        if(i + 1 >= ArraySize(g_CloseBuffer)) continue;
        if(i + 1 >= ArraySize(g_HighBuffer)) continue;
        
        double currClose = g_CloseBuffer[i];      // 当前棒收盘
        double prevHigh = g_HighBuffer[i + 1];    // 前一棒高点
        
        // 收盘价高于前一棒高点 = 多头夺回控制权
        if(currClose > prevHigh)
        {
            return true;
        }
    }
    
    return false;
}

//+------------------------------------------------------------------+
//| Check Bear Confirmation (检查空头确认 - Swing High 右侧)            |
//| 条件 B：右侧至少有一根棒线的收盘价低于前一根棒线的低点                  |
//| 表示空头不仅是插针，而且夺回了控制权                                   |
//+------------------------------------------------------------------+
bool CheckBearConfirmation(int swingBarIndex)
{
    for(int i = swingBarIndex - 1; i >= 1; i--)
    {
        if(i + 1 >= ArraySize(g_CloseBuffer)) continue;
        if(i + 1 >= ArraySize(g_LowBuffer)) continue;
        
        double currClose = g_CloseBuffer[i];      // 当前棒收盘
        double prevLow = g_LowBuffer[i + 1];      // 前一棒低点
        
        // 收盘价低于前一棒低点 = 空头夺回控制权
        if(currClose < prevLow)
        {
            return true;
        }
    }
    
    return false;
}

//+------------------------------------------------------------------+
//| Is Range Minimum (检查是否是区间最低点)                              |
//| 条件 A：Price[i] 是 i-N 到 i+N 范围内的最低点                        |
//+------------------------------------------------------------------+
bool IsRangeMinimum(int barIndex, int depth)
{
    if(barIndex - depth < 1) return false;  // 右侧数据不足
    if(barIndex + depth >= ArraySize(g_LowBuffer)) return false;  // 左侧数据不足
    
    double centerLow = g_LowBuffer[barIndex];
    
    // 检查左侧 N 根棒
    for(int i = 1; i <= depth; i++)
    {
        if(g_LowBuffer[barIndex + i] < centerLow)
            return false;  // 左侧有更低的点
    }
    
    // 检查右侧 N 根棒
    for(int i = 1; i <= depth; i++)
    {
        if(g_LowBuffer[barIndex - i] < centerLow)
            return false;  // 右侧有更低的点
    }
    
    return true;
}

//+------------------------------------------------------------------+
//| Is Range Maximum (检查是否是区间最高点)                              |
//| 条件 A：Price[i] 是 i-N 到 i+N 范围内的最高点                        |
//+------------------------------------------------------------------+
bool IsRangeMaximum(int barIndex, int depth)
{
    if(barIndex - depth < 1) return false;
    if(barIndex + depth >= ArraySize(g_HighBuffer)) return false;
    
    double centerHigh = g_HighBuffer[barIndex];
    
    // 检查左侧 N 根棒
    for(int i = 1; i <= depth; i++)
    {
        if(g_HighBuffer[barIndex + i] > centerHigh)
            return false;
    }
    
    // 检查右侧 N 根棒
    for(int i = 1; i <= depth; i++)
    {
        if(g_HighBuffer[barIndex - i] > centerHigh)
            return false;
    }
    
    return true;
}

//+------------------------------------------------------------------+
//| Find Swing Low (寻找最近的有效摆动低点)                              |
//| Al Brooks: 技术止损应该放在最近的支撑位下方                          |
//| 动态强度 + 双重确认逻辑：                                            |
//|   条件 A：区间最低点（i-N 到 i+N 范围内最低）                         |
//|   条件 B：收盘确认（右侧有多头夺回控制权的棒线）                       |
//+------------------------------------------------------------------+
double FindSwingLow(int lookback)
{
    if(lookback < 3) return 0;
    if(ArraySize(g_LowBuffer) < lookback + 10) return 0;
    
    // 获取动态探测深度
    int depth = GetSwingDepth();
    
    double validSwingLow = 0;
    int validSwingBarIndex = -1;
    
    // 从 bar[depth+1] 开始向回搜索（需要保证右侧有足够的确认空间）
    int startBar = depth + 1;
    int endBar = lookback;
    
    for(int i = startBar; i <= endBar; i++)
    {
        // 条件 A：检查是否是区间最低点
        if(!IsRangeMinimum(i, depth))
            continue;
        
        // 条件 B：检查多头确认（右侧是否有收盘高于前一棒高点的棒线）
        if(!CheckBullConfirmation(i))
            continue;
        
        // 双重确认通过，找到有效 Swing Low
        double swingPrice = g_LowBuffer[i];
        
        // 取最近的有效 Swing Low（第一个找到的）
        if(validSwingLow == 0)
        {
            validSwingLow = swingPrice;
            validSwingBarIndex = i;
            
            Print("📍 有效 Swing Low: ", DoubleToString(validSwingLow, g_SymbolDigits),
                  " | Bar[", i, "] | 深度=", depth,
                  " | 状态=", GetMarketStateString(g_MarketState));
            break;  // 找到最近的一个即可
        }
    }
    
    // 如果没有找到有效 Swing Low，返回 0（让调用者回退到 ATR 止损）
    if(validSwingLow == 0)
    {
        Print("📍 未找到有效 Swing Low (深度=", depth, ")，将使用 ATR 止损");
    }
    
    return validSwingLow;
}

//+------------------------------------------------------------------+
//| Find Swing High (寻找最近的有效摆动高点)                             |
//| Al Brooks: 技术止损应该放在最近的阻力位上方                          |
//| 动态强度 + 双重确认逻辑：                                            |
//|   条件 A：区间最高点（i-N 到 i+N 范围内最高）                         |
//|   条件 B：收盘确认（右侧有空头夺回控制权的棒线）                       |
//+------------------------------------------------------------------+
double FindSwingHigh(int lookback)
{
    if(lookback < 3) return 0;
    if(ArraySize(g_HighBuffer) < lookback + 10) return 0;
    
    // 获取动态探测深度
    int depth = GetSwingDepth();
    
    double validSwingHigh = 0;
    int validSwingBarIndex = -1;
    
    int startBar = depth + 1;
    int endBar = lookback;
    
    for(int i = startBar; i <= endBar; i++)
    {
        // 条件 A：检查是否是区间最高点
        if(!IsRangeMaximum(i, depth))
            continue;
        
        // 条件 B：检查空头确认（右侧是否有收盘低于前一棒低点的棒线）
        if(!CheckBearConfirmation(i))
            continue;
        
        // 双重确认通过
        double swingPrice = g_HighBuffer[i];
        
        if(validSwingHigh == 0)
        {
            validSwingHigh = swingPrice;
            validSwingBarIndex = i;
            
            Print("📍 有效 Swing High: ", DoubleToString(validSwingHigh, g_SymbolDigits),
                  " | Bar[", i, "] | 深度=", depth,
                  " | 状态=", GetMarketStateString(g_MarketState));
            break;
        }
    }
    
    if(validSwingHigh == 0)
    {
        Print("📍 未找到有效 Swing High (深度=", depth, ")，将使用 ATR 止损");
    }
    
    return validSwingHigh;
}

//+------------------------------------------------------------------+
//| Calculate Stop Loss (兼容旧调用)                                    |
//+------------------------------------------------------------------+
double CalculateStopLoss(string side, double atr)
{
    double entryPrice = side == "buy" ? 
                        SymbolInfoDouble(_Symbol, SYMBOL_ASK) : 
                        SymbolInfoDouble(_Symbol, SYMBOL_BID);
    return CalculateUnifiedStopLoss(side, atr, entryPrice);
}

//+------------------------------------------------------------------+
//| Check Signal Cooldown                                             |
//+------------------------------------------------------------------+
bool CheckSignalCooldown(string side)
{
    int currentBar = Bars(_Symbol, PERIOD_CURRENT);
    
    if(side == "buy")
    {
        if(currentBar - g_LastBuySignalBar < InpSignalCooldown)
            return false;
    }
    else
    {
        if(currentBar - g_LastSellSignalBar < InpSignalCooldown)
            return false;
    }
    
    return true;
}

//+------------------------------------------------------------------+
//| Update Signal Cooldown                                            |
//+------------------------------------------------------------------+
void UpdateSignalCooldown(string side)
{
    int currentBar = Bars(_Symbol, PERIOD_CURRENT);
    
    if(side == "buy")
    {
        g_LastBuySignalBar = currentBar;
        g_LastBuySignalTime = TimeCurrent();
    }
    else
    {
        g_LastSellSignalBar = currentBar;
        g_LastSellSignalTime = TimeCurrent();
    }
}

//+------------------------------------------------------------------+
//| Determine Order Type (动态订单类型分配)                            |
//| Spike 模式：市价单（入场 > 价格）                                   |
//| Pullback 模式：限价单（价格 > 成本）                                |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE DetermineOrderType(ENUM_SIGNAL_TYPE signal, string side)
{
    // 默认为市价单
    bool useMarketOrder = true;
    
    // 判断是否为 Spike 模式（Urgency - 入场比价格更重要）
    bool isSpikeMode = (signal == SIGNAL_SPIKE_MARKET_BUY || 
                        signal == SIGNAL_SPIKE_MARKET_SELL ||
                        signal == SIGNAL_EMERGENCY_SPIKE_BUY ||
                        signal == SIGNAL_EMERGENCY_SPIKE_SELL ||
                        signal == SIGNAL_SPIKE_BUY || 
                        signal == SIGNAL_SPIKE_SELL);
    
    // 判断是否为 Pullback 模式（Value - 限价单抵消点差成本）
    bool isPullbackMode = (signal == SIGNAL_H1_BUY || signal == SIGNAL_H2_BUY ||
                           signal == SIGNAL_L1_SELL || signal == SIGNAL_L2_SELL ||
                           signal == SIGNAL_MICRO_CH_H1_BUY || signal == SIGNAL_MICRO_CH_H1_SELL);
    
    // Spike 模式：使用市价单
    if(isSpikeMode)
    {
        useMarketOrder = true;
    }
    // Pullback 模式：使用限价单（如果启用）
    else if(isPullbackMode && InpUseLimitOrders)
    {
        useMarketOrder = false;
    }
    
    // 返回订单类型
    if(useMarketOrder)
    {
        return side == "buy" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
    }
    else
    {
        return side == "buy" ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT;
    }
}

//+------------------------------------------------------------------+
//| Calculate Limit Order Price (计算限价单价格)                       |
//| 使用前一根 K 线极值或信号棒极值                                     |
//+------------------------------------------------------------------+
double CalculateLimitOrderPrice(string side)
{
    // 使用前一根 K 线的极值作为限价单价格
    // 这样可以抵消点差带来的成本
    double limitPrice = 0;
    
    if(side == "buy")
    {
        // 买入限价单：设在前一棒高点（等待回调后突破）
        // 或者使用信号棒高点
        limitPrice = g_HighBuffer[1];  // Entry Bar 高点
        
        // 可选：添加一点偏移
        if(InpLimitOrderOffset > 0)
            limitPrice += InpLimitOrderOffset * g_SymbolPoint;
    }
    else
    {
        // 卖出限价单：设在前一棒低点
        limitPrice = g_LowBuffer[1];  // Entry Bar 低点
        
        // 可选：添加一点偏移
        if(InpLimitOrderOffset > 0)
            limitPrice -= InpLimitOrderOffset * g_SymbolPoint;
    }
    
    return NormalizeDouble(limitPrice, g_SymbolDigits);
}

//+------------------------------------------------------------------+
//| Process Signal (处理信号) - 使用 CTrade 类下单                      |
//| 支持动态订单类型分配：市价单 / 限价单                               |
//| 【新增】混合止损机制 (Hybrid Stop Mechanism)                        |
//|   - 硬止损：放宽后发送到服务器，作为灾难保护线                        |
//|   - 软止损：EA 监控原始技术位，收盘破坏则市价平仓                     |
//+------------------------------------------------------------------+
void ProcessSignal(ENUM_SIGNAL_TYPE signal, double stopLoss, double baseHeight)
{
    if(!InpEnableTrading) 
    {
        Print("ℹ️ 交易未启用 - 信号: ", SignalTypeToString(signal));
        return;
    }
    
    // 检查现有持仓
    int positions = CountPositions();
    if(positions >= InpMaxPositions) 
    {
        Print("ℹ️ 已达最大持仓数 (", positions, "/", InpMaxPositions, ") - 信号: ", SignalTypeToString(signal));
        return;
    }
    
    // 获取信号方向
    string side = GetSignalSide(signal);
    if(side == "") return;
    
    string signalName = SignalTypeToString(signal);
    
    //=================================================================
    // 动态订单类型分配
    //=================================================================
    ENUM_ORDER_TYPE orderType = DetermineOrderType(signal, side);
    bool isMarketOrder = (orderType == ORDER_TYPE_BUY || orderType == ORDER_TYPE_SELL);
    bool isLimitOrder = (orderType == ORDER_TYPE_BUY_LIMIT || orderType == ORDER_TYPE_SELL_LIMIT);
    
    //=================================================================
    // 计算入场价格
    //=================================================================
    double entryPrice = 0;
    double limitPrice = 0;
    
    if(isMarketOrder)
    {
        if(side == "buy")
            entryPrice = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        else
            entryPrice = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    }
    else if(isLimitOrder)
    {
        limitPrice = CalculateLimitOrderPrice(side);
        entryPrice = limitPrice;
    }
    
    //=================================================================
    // 验证止损（硬性约束：不得超过 3×ATR）
    //=================================================================
    if(stopLoss <= 0)
    {
        Print("❌ ", signalName, " - 无效止损");
        return;
    }
    
    double atr = g_ATRBuffer[1];
    double risk = side == "buy" ? (entryPrice - stopLoss) : (stopLoss - entryPrice);
    
    if(risk <= 0)
    {
        Print("❌ ", signalName, " - 风险计算无效 (risk=", DoubleToString(risk, g_SymbolDigits), ")");
        return;
    }
    
    if(atr > 0 && risk > atr * InpMaxStopATRMult)
    {
        Print("❌ ", signalName, " - 止损距离 ", DoubleToString(risk, g_SymbolDigits), 
              " 超过硬性约束 ", InpMaxStopATRMult, "×ATR = ", DoubleToString(atr * InpMaxStopATRMult, g_SymbolDigits));
        return;
    }
    
    //=================================================================
    // 【混合止损机制】保存原始技术止损位（用于软止损）
    //=================================================================
    double technicalSL = stopLoss;  // 原始技术止损位
    double brokerSL = 0;            // 发送给 Broker 的硬止损
    
    if(InpEnableHardStop)
    {
        // 硬止损：放宽后的灾难保护线
        double extraBuffer = risk * (InpHardStopBufferMult - 1.0);
        if(side == "buy")
            brokerSL = stopLoss - extraBuffer;
        else
            brokerSL = stopLoss + extraBuffer;
        
        brokerSL = NormalizeDouble(brokerSL, g_SymbolDigits);
        
        Print("🛡️ 混合止损: 技术位=", DoubleToString(technicalSL, g_SymbolDigits),
              " | 硬止损(Broker)=", DoubleToString(brokerSL, g_SymbolDigits),
              " (放宽 ", DoubleToString(InpHardStopBufferMult, 1), "倍)");
    }
    else
    {
        // 不启用硬止损：SL 填 0
        brokerSL = 0;
        Print("🛡️ 混合止损: 技术位=", DoubleToString(technicalSL, g_SymbolDigits),
              " | 硬止损=禁用 (无服务器止损)");
    }
    
    //=================================================================
    // 动态止盈 TP1 (Al Brooks 等距测算 + 状态调节)
    // 基础高度: SignalBarBody = |Open[2] - Close[2]|
    // 公式: TP1_Dist = MathMax(ATR * InpTP1Multiplier, SignalBarBody) * 状态调节乘数
    // 强趋势 1.2 → 博取等距利润；震荡 0.7 → 快速落袋
    //=================================================================
    double tp1 = 0, tp2 = 0;
    
    // 信号棒实体高度（bar[2] = 触发信号的棒线）
    double signalBarOpen   = g_OpenBuffer[2];
    double signalBarClose = g_CloseBuffer[2];
    double signalBarBody  = MathAbs(signalBarClose - signalBarOpen);
    double signalBarHigh  = g_HighBuffer[2];
    double signalBarLow   = g_LowBuffer[2];
    
    // 基础高度：取 ATR 参考与信号棒实体较大者
    double atrBase = (atr > 0) ? atr * InpTP1Multiplier : signalBarBody;
    double tp1BaseHeight = MathMax(atrBase, signalBarBody);
    
    // 状态调节乘数
    double stateMultiplier = 1.0;
    string stateLabel = "标准(1.0)";
    
    if(g_MarketState == MARKET_STATE_STRONG_TREND ||
       g_MarketState == MARKET_STATE_BREAKOUT ||
       g_MarketCycle == MARKET_CYCLE_SPIKE)
    {
        stateMultiplier = 1.2;
        stateLabel = "强趋势(1.2)";
    }
    else if(g_MarketState == MARKET_STATE_TRADING_RANGE)
    {
        stateMultiplier = 0.7;
        stateLabel = "震荡(0.7)";
    }
    
    // TP1 距离 = 基础高度 × 状态调节乘数
    double tp1Distance = tp1BaseHeight * stateMultiplier;
    double tp2Distance = risk * InpTP2RiskMultiple;
    string tp1Method = "动态止盈 [" + stateLabel + "]";
    
    Print("📐 动态TP1: Base=", DoubleToString(tp1BaseHeight, g_SymbolDigits),
          " (ATR×", DoubleToString(InpTP1Multiplier, 1), "=", DoubleToString(atrBase, g_SymbolDigits),
          " vs Body=", DoubleToString(signalBarBody, g_SymbolDigits),
          ") × ", stateLabel, " = ", DoubleToString(tp1Distance, g_SymbolDigits));
    
    if(side == "buy")
    {
        tp1 = entryPrice + tp1Distance;
        tp2 = entryPrice + tp2Distance;
    }
    else
    {
        tp1 = entryPrice - tp1Distance;
        tp2 = entryPrice - tp2Distance;
    }
    
    //=================================================================
    // 规范化所有价格
    //=================================================================
    technicalSL = NormalizeDouble(technicalSL, g_SymbolDigits);
    tp1 = NormalizeDouble(tp1, g_SymbolDigits);
    tp2 = NormalizeDouble(tp2, g_SymbolDigits);
    entryPrice = NormalizeDouble(entryPrice, g_SymbolDigits);
    if(isLimitOrder)
        limitPrice = NormalizeDouble(limitPrice, g_SymbolDigits);
    
    //=================================================================
    // 检查最小止损距离（broker 限制）
    //=================================================================
    if(InpEnableHardStop && brokerSL > 0)
    {
        long stopLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
        double minStopDistance = stopLevel * g_SymbolPoint;
        
        if(MathAbs(entryPrice - brokerSL) < minStopDistance)
        {
            Print("⚠️ 硬止损距离小于 broker 最小要求 (", stopLevel, " points)");
            if(side == "buy")
                brokerSL = entryPrice - minStopDistance - g_SymbolPoint;
            else
                brokerSL = entryPrice + minStopDistance + g_SymbolPoint;
            brokerSL = NormalizeDouble(brokerSL, g_SymbolDigits);
        }
    }
    
    //=================================================================
    // 使用 CTrade 类下单
    //=================================================================
    bool result = false;
    string comment = signalName + "_" + TimeToString(TimeCurrent(), TIME_MINUTES);
    string orderTypeStr = "";
    
    trade.SetExpertMagicNumber(InpMagicNumber);
    trade.SetDeviationInPoints(10);
    
    if(isMarketOrder)
    {
        if(side == "buy")
        {
            result = trade.Buy(InpLotSize, _Symbol, 0, brokerSL, tp2, comment);
            orderTypeStr = "市价买入";
        }
        else
        {
            result = trade.Sell(InpLotSize, _Symbol, 0, brokerSL, tp2, comment);
            orderTypeStr = "市价卖出";
        }
    }
    else if(isLimitOrder)
    {
        datetime expiration = TimeCurrent() + PeriodSeconds(PERIOD_CURRENT) * 5;
        
        if(side == "buy")
        {
            result = trade.BuyLimit(InpLotSize, limitPrice, _Symbol, brokerSL, tp2, 
                                    ORDER_TIME_SPECIFIED, expiration, comment);
            orderTypeStr = "限价买入";
        }
        else
        {
            result = trade.SellLimit(InpLotSize, limitPrice, _Symbol, brokerSL, tp2,
                                     ORDER_TIME_SPECIFIED, expiration, comment);
            orderTypeStr = "限价卖出";
        }
    }
    
    //=================================================================
    // 处理结果
    //=================================================================
    if(result)
    {
        ulong ticket = trade.ResultOrder();
        double actualPrice = trade.ResultPrice();
        
        //=============================================================
        // 【混合止损】将原始技术止损位添加到软止损列表
        //=============================================================
        if(InpEnableSoftStop)
        {
            AddSoftStopInfo(ticket, technicalSL, side);
        }
        
        // 记录 TP1 价格（动态止盈触发用）
        AddTP1Info(ticket, tp1, side);
        
        Print("═══════════════════════════════════════════════════════════════");
        Print("✅ ", signalName, " 下单成功");
        Print("   订单类型: ", orderTypeStr);
        Print("   订单号: ", ticket);
        Print("   方向: ", side == "buy" ? "做多" : "做空");
        if(isMarketOrder)
            Print("   入场价: ", DoubleToString(actualPrice > 0 ? actualPrice : entryPrice, g_SymbolDigits));
        else
            Print("   限价: ", DoubleToString(limitPrice, g_SymbolDigits));
        
        // 混合止损信息
        Print("   ─────────────────────────────────────");
        Print("   🛡️ 混合止损机制:");
        Print("      技术止损(软): ", DoubleToString(technicalSL, g_SymbolDigits), 
              " (EA监控，收盘破坏则平仓)");
        if(InpEnableHardStop)
            Print("      硬止损(Broker): ", DoubleToString(brokerSL, g_SymbolDigits),
                  " (灾难保护线，放宽", DoubleToString(InpHardStopBufferMult, 1), "倍)");
        else
            Print("      硬止损(Broker): 禁用");
        Print("   ─────────────────────────────────────");
        
        Print("   风险: ", DoubleToString(risk, g_SymbolDigits));
        Print("   TP1: ", DoubleToString(tp1, g_SymbolDigits), 
              " [", tp1Method, "] 距离=", DoubleToString(tp1Distance, g_SymbolDigits));
        Print("   TP2: ", DoubleToString(tp2, g_SymbolDigits), " (", DoubleToString(InpTP2RiskMultiple, 1), "R)");
        Print("   手数: ", InpLotSize);
        Print("   点差: ", DoubleToString(g_CurrentSpread, 1), " 点 | 平均: ", DoubleToString(g_AverageSpread, 1), " 点");
        Print("   时段: ", g_CurrentSession);
        Print("═══════════════════════════════════════════════════════════════");
    }
    else
    {
        uint errorCode = trade.ResultRetcode();
        string errorDesc = trade.ResultRetcodeDescription();
        
        Print("═══════════════════════════════════════════════════════════════");
        Print("❌ ", signalName, " 开仓失败");
        Print("   错误代码: ", errorCode);
        Print("   错误描述: ", errorDesc);
        Print("   尝试入场价: ", DoubleToString(entryPrice, _Digits));
        Print("   硬止损: ", DoubleToString(brokerSL, _Digits));
        Print("   TP2: ", DoubleToString(tp2, _Digits));
        Print("═══════════════════════════════════════════════════════════════");
    }
}

//+------------------------------------------------------------------+
//| Add Soft Stop Info (添加软止损信息到列表)                          |
//| 增强版：防止重复添加、容量检查                                       |
//+------------------------------------------------------------------+
void AddSoftStopInfo(ulong ticket, double technicalSL, string side)
{
    // 防止重复添加
    for(int i = 0; i < g_SoftStopCount; i++)
    {
        if(g_SoftStopList[i].ticket == ticket)
        {
            Print("📋 软止损列表: 订单 #", ticket, " 已存在，跳过添加");
            return;
        }
    }
    
    // 容量保护（最大 100 条记录，防止异常情况）
    const int MAX_SOFT_STOP_RECORDS = 100;
    if(g_SoftStopCount >= MAX_SOFT_STOP_RECORDS)
    {
        Print("⚠️ 软止损列表已满 (", MAX_SOFT_STOP_RECORDS, ")，触发强制清理");
        SyncSoftStopList();  // 强制同步清理
        
        // 清理后仍然满，则拒绝添加
        if(g_SoftStopCount >= MAX_SOFT_STOP_RECORDS)
        {
            Print("❌ 软止损列表清理后仍满，无法添加订单 #", ticket);
            return;
        }
    }
    
    // 扩展数组
    int newSize = g_SoftStopCount + 1;
    ArrayResize(g_SoftStopList, newSize);
    
    // 添加新记录
    g_SoftStopList[g_SoftStopCount].ticket = ticket;
    g_SoftStopList[g_SoftStopCount].technicalSL = technicalSL;
    g_SoftStopList[g_SoftStopCount].side = side;
    g_SoftStopCount++;
    
    Print("📋 软止损列表: 添加订单 #", ticket, 
          " | 技术位=", DoubleToString(technicalSL, g_SymbolDigits),
          " | 当前数量=", g_SoftStopCount);
}

//+------------------------------------------------------------------+
//| Remove Soft Stop Info (从列表移除软止损信息)                       |
//+------------------------------------------------------------------+
void RemoveSoftStopInfo(ulong ticket)
{
    for(int i = 0; i < g_SoftStopCount; i++)
    {
        if(g_SoftStopList[i].ticket == ticket)
        {
            // 移动后面的元素
            for(int j = i; j < g_SoftStopCount - 1; j++)
            {
                g_SoftStopList[j] = g_SoftStopList[j + 1];
            }
            g_SoftStopCount--;
            
            // 缩小数组（最小保留 1 个元素的空间）
            int newSize = g_SoftStopCount > 0 ? g_SoftStopCount : 1;
            ArrayResize(g_SoftStopList, newSize);
            
            Print("📋 软止损列表: 移除订单 #", ticket, " | 剩余数量=", g_SoftStopCount);
            return;
        }
    }
    // 如果没找到，不输出日志（可能已被清理）
}

//+------------------------------------------------------------------+
//| Sync Soft Stop List (同步软止损列表与实际持仓)                      |
//| 健壮性保证：清理所有无效记录（持仓已不存在的）                        |
//+------------------------------------------------------------------+
void SyncSoftStopList()
{
    if(g_SoftStopCount == 0) return;
    
    int removedCount = 0;
    
    // 从后往前遍历，安全删除
    for(int i = g_SoftStopCount - 1; i >= 0; i--)
    {
        ulong ticket = g_SoftStopList[i].ticket;
        
        // 检查持仓是否存在
        bool positionExists = PositionSelectByTicket(ticket);
        bool magicMatches = positionExists && 
                            (PositionGetInteger(POSITION_MAGIC) == InpMagicNumber);
        
        if(!positionExists || !magicMatches)
        {
            // 直接移除（不调用 RemoveSoftStopInfo 避免重复日志）
            for(int j = i; j < g_SoftStopCount - 1; j++)
            {
                g_SoftStopList[j] = g_SoftStopList[j + 1];
            }
            g_SoftStopCount--;
            removedCount++;
        }
    }
    
    // 调整数组大小
    if(removedCount > 0)
    {
        int newSize = g_SoftStopCount > 0 ? g_SoftStopCount : 1;
        ArrayResize(g_SoftStopList, newSize);
        Print("📋 软止损列表同步: 清理 ", removedCount, " 条无效记录 | 剩余=", g_SoftStopCount);
    }
}

//+------------------------------------------------------------------+
//| Add TP1 Info (记录 TP1 价格，用于动态止盈触发)                      |
//+------------------------------------------------------------------+
void AddTP1Info(ulong ticket, double tp1Price, string side)
{
    for(int i = 0; i < g_TP1Count; i++)
    {
        if(g_TP1List[i].ticket == ticket) return;  // 已存在
    }
    
    if(g_TP1Count >= MAX_TP1_RECORDS)
    {
        // 简单压缩：移除第一条
        for(int i = 0; i < g_TP1Count - 1; i++)
            g_TP1List[i] = g_TP1List[i + 1];
        g_TP1Count--;
    }
    
    int newSize = g_TP1Count + 1;
    ArrayResize(g_TP1List, newSize);
    g_TP1List[g_TP1Count].ticket = ticket;
    g_TP1List[g_TP1Count].tp1Price = tp1Price;
    g_TP1List[g_TP1Count].side = side;
    g_TP1Count++;
}

//+------------------------------------------------------------------+
//| Remove TP1 Info (移除 TP1 记录)                                    |
//+------------------------------------------------------------------+
void RemoveTP1Info(ulong ticket)
{
    for(int i = 0; i < g_TP1Count; i++)
    {
        if(g_TP1List[i].ticket == ticket)
        {
            for(int j = i; j < g_TP1Count - 1; j++)
                g_TP1List[j] = g_TP1List[j + 1];
            g_TP1Count--;
            ArrayResize(g_TP1List, g_TP1Count > 0 ? g_TP1Count : 1);
            return;
        }
    }
}

//+------------------------------------------------------------------+
//| Get TP1 Price (获取持仓的 TP1 价格，用于判断是否触发)               |
//+------------------------------------------------------------------+
double GetTP1Price(ulong ticket)
{
    for(int i = 0; i < g_TP1Count; i++)
    {
        if(g_TP1List[i].ticket == ticket)
            return g_TP1List[i].tp1Price;
    }
    return 0;
}

//+------------------------------------------------------------------+
//| Get TP1 Side (获取持仓方向，用于 TP1 触发判断)                      |
//+------------------------------------------------------------------+
string GetTP1Side(ulong ticket)
{
    for(int i = 0; i < g_TP1Count; i++)
    {
        if(g_TP1List[i].ticket == ticket)
            return g_TP1List[i].side;
    }
    return "";
}

//+------------------------------------------------------------------+
//| Sync TP1 List (移除已平仓订单的 TP1 记录)                           |
//+------------------------------------------------------------------+
void SyncTP1List()
{
    for(int i = g_TP1Count - 1; i >= 0; i--)
    {
        if(!PositionSelectByTicket(g_TP1List[i].ticket))
        {
            for(int j = i; j < g_TP1Count - 1; j++)
                g_TP1List[j] = g_TP1List[j + 1];
            g_TP1Count--;
        }
    }
    if(g_TP1Count >= 0)
        ArrayResize(g_TP1List, g_TP1Count > 0 ? g_TP1Count : 1);
}

//+------------------------------------------------------------------+
//| Check Soft Stop Exit (检查软止损 - 收盘价逻辑止损)                  |
//| Al Brooks: 交易前提失效 (Premise Failed) 则立即离场                 |
//| 做多：如果收盘价 < 原始技术止损位，说明结构被破坏                     |
//| 做空：如果收盘价 > 原始技术止损位，说明结构被破坏                     |
//+------------------------------------------------------------------+
void CheckSoftStopExit()
{
    if(!InpEnableSoftStop) return;
    if(g_SoftStopCount == 0) return;
    
    // 定期同步检查（每 10 根 K 线同步一次，确保列表健康）
    static int syncCounter = 0;
    syncCounter++;
    if(syncCounter >= 10)
    {
        SyncSoftStopList();
        syncCounter = 0;
    }
    
    // 获取前一根 K 线收盘价
    double prevClose = g_CloseBuffer[1];
    
    // 遍历所有软止损记录（从后往前，安全删除）
    for(int i = g_SoftStopCount - 1; i >= 0; i--)
    {
        ulong ticket = g_SoftStopList[i].ticket;
        double technicalSL = g_SoftStopList[i].technicalSL;
        string side = g_SoftStopList[i].side;
        
        // 检查持仓是否还存在
        if(!PositionSelectByTicket(ticket))
        {
            // 持仓已不存在（可能被硬止损打掉），移除记录
            RemoveSoftStopInfo(ticket);
            continue;
        }
        
        // 验证 Magic Number
        if(PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
        {
            RemoveSoftStopInfo(ticket);
            continue;
        }
        
        bool shouldClose = false;
        
        if(side == "buy")
        {
            // 做多：收盘价 < 技术止损位 = 结构破坏
            if(prevClose < technicalSL)
            {
                shouldClose = true;
                Print("⚠️ 逻辑止损触发 [做多] #", ticket);
                Print("   K线收盘 ", DoubleToString(prevClose, g_SymbolDigits), 
                      " < 技术止损位 ", DoubleToString(technicalSL, g_SymbolDigits));
                Print("   交易前提失效 (Premise Failed)，市价离场");
            }
        }
        else if(side == "sell")
        {
            // 做空：收盘价 > 技术止损位 = 结构破坏
            if(prevClose > technicalSL)
            {
                shouldClose = true;
                Print("⚠️ 逻辑止损触发 [做空] #", ticket);
                Print("   K线收盘 ", DoubleToString(prevClose, g_SymbolDigits), 
                      " > 技术止损位 ", DoubleToString(technicalSL, g_SymbolDigits));
                Print("   交易前提失效 (Premise Failed)，市价离场");
            }
        }
        
        // 执行市价平仓
        if(shouldClose)
        {
            if(trade.PositionClose(ticket))
            {
                Print("✅ 逻辑止损平仓成功 #", ticket);
                RemoveSoftStopInfo(ticket);
            }
            else
            {
                Print("❌ 逻辑止损平仓失败 #", ticket, " | 错误: ", trade.ResultRetcodeDescription());
            }
        }
    }
}

//+------------------------------------------------------------------+
//| Get Signal Side (获取信号方向)                                      |
//+------------------------------------------------------------------+
string GetSignalSide(ENUM_SIGNAL_TYPE signal)
{
    switch(signal)
    {
        // 买入信号
        case SIGNAL_SPIKE_MARKET_BUY:
        case SIGNAL_EMERGENCY_SPIKE_BUY:
        case SIGNAL_MICRO_CH_H1_BUY:
        case SIGNAL_SPIKE_BUY:
        case SIGNAL_H1_BUY:
        case SIGNAL_H2_BUY:
        case SIGNAL_WEDGE_BUY:
        case SIGNAL_CLIMAX_BUY:
        case SIGNAL_MTR_BUY:
        case SIGNAL_FAILED_BO_BUY:
        case SIGNAL_GAPBAR_BUY:
        case SIGNAL_FINAL_FLAG_BUY:
            return "buy";
            
        // 卖出信号
        case SIGNAL_SPIKE_MARKET_SELL:
        case SIGNAL_EMERGENCY_SPIKE_SELL:
        case SIGNAL_MICRO_CH_H1_SELL:
        case SIGNAL_SPIKE_SELL:
        case SIGNAL_L1_SELL:
        case SIGNAL_L2_SELL:
        case SIGNAL_WEDGE_SELL:
        case SIGNAL_CLIMAX_SELL:
        case SIGNAL_MTR_SELL:
        case SIGNAL_FAILED_BO_SELL:
        case SIGNAL_GAPBAR_SELL:
        case SIGNAL_FINAL_FLAG_SELL:
            return "sell";
            
        default:
            return "";
    }
}

//+------------------------------------------------------------------+
//| Manage Positions (仓位管理)                                        |
//| - TP1 触及时平仓 50%（按记录的动态 TP1 价格触发）                     |
//| - 将止损移动至保本位                                                |
//+------------------------------------------------------------------+
void ManagePositions(double ema, double atr)
{
    SyncTP1List();  // 清理已平仓订单的 TP1 记录
    
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        if(!positionInfo.SelectByIndex(i)) continue;
        if(positionInfo.Magic() != InpMagicNumber) continue;
        if(positionInfo.Symbol() != _Symbol) continue;
        
        ulong ticket = positionInfo.Ticket();
        double positionPrice = positionInfo.PriceOpen();
        double positionSL = positionInfo.StopLoss();
        double positionTP = positionInfo.TakeProfit();
        double positionVolume = positionInfo.Volume();
        long positionType = positionInfo.PositionType();
        string positionComment = positionInfo.Comment();
        
        // 获取当前价格
        double currentPrice = positionType == POSITION_TYPE_BUY ? 
                              SymbolInfoDouble(_Symbol, SYMBOL_BID) :
                              SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        
        // 计算原始风险（入场价到止损的距离）
        double risk = positionType == POSITION_TYPE_BUY ? 
                      (positionPrice - positionSL) : (positionSL - positionPrice);
        
        // 如果没有有效止损，跳过
        if(risk <= 0) 
        {
            // 尝试设置止损
            if(positionSL == 0 && atr > 0)
            {
                double emergencySL = 0;
                if(positionType == POSITION_TYPE_BUY)
                    emergencySL = positionPrice - atr * 2.0;
                else
                    emergencySL = positionPrice + atr * 2.0;
                
                emergencySL = NormalizeDouble(emergencySL, _Digits);
                
                if(trade.PositionModify(ticket, emergencySL, positionTP))
                    Print("⚠️ 为订单 ", ticket, " 设置紧急止损: ", DoubleToString(emergencySL, _Digits));
            }
            continue;
        }
        
        // 计算当前盈亏倍数 (R-Multiple)
        double currentRR = 0;
        if(positionType == POSITION_TYPE_BUY)
            currentRR = (currentPrice - positionPrice) / risk;
        else
            currentRR = (positionPrice - currentPrice) / risk;
        
        //=================================================================
        // TP1 触发：平仓 50% 并移动止损到保本位
        // 优先使用记录的动态 TP1 价格，无记录时按 0.8R 兜底
        //=================================================================
        // 检查是否已经触发过 TP1（通过检查止损是否已经移动到保本位附近）
        bool alreadyTP1 = false;
        if(positionType == POSITION_TYPE_BUY)
            alreadyTP1 = positionSL >= positionPrice - _Point * 5;
        else
            alreadyTP1 = positionSL <= positionPrice + _Point * 5;
        
        // 是否达到 TP1：有记录则按价格，无记录则按 0.8R
        bool tp1Reached = false;
        double storedTP1 = GetTP1Price(ticket);
        if(storedTP1 > 0)
        {
            string tp1Side = GetTP1Side(ticket);
            if(tp1Side == "buy")
                tp1Reached = (currentPrice >= storedTP1);
            else
                tp1Reached = (currentPrice <= storedTP1);
        }
        else
            tp1Reached = (currentRR >= 0.8);  // 兜底：无记录时 0.8R
        
        // 最小剩余手数检查
        double volumeMin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
        double volumeStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
        
        if(tp1Reached && !alreadyTP1 && positionVolume > volumeMin)
        {
            // 计算 50% 平仓量
            double closeVolume = NormalizeDouble(positionVolume * (InpTP1ClosePercent / 100.0), 2);
            
            // 确保符合最小手数和步进要求
            if(closeVolume < volumeMin)
                closeVolume = volumeMin;
            
            // 确保剩余手数 >= 最小手数
            if(positionVolume - closeVolume < volumeMin)
                closeVolume = positionVolume - volumeMin;
            
            // 按步进调整
            if(volumeStep > 0)
                closeVolume = MathFloor(closeVolume / volumeStep) * volumeStep;
            
            closeVolume = NormalizeDouble(closeVolume, 2);
            
            if(closeVolume >= volumeMin)
            {
                // 部分平仓
                if(trade.PositionClosePartial(ticket, closeVolume))
                {
                    Print("═══════════════════════════════════════════════════════════════");
                    Print("✅ TP1 触发 - 平仓 50%");
                    Print("   订单号: ", ticket);
                    Print("   平仓量: ", DoubleToString(closeVolume, 2), " 手");
                    Print("   剩余量: ", DoubleToString(positionVolume - closeVolume, 2), " 手");
                    Print("   当前 R: ", DoubleToString(currentRR, 2), "R");
                    Print("   当前价: ", DoubleToString(currentPrice, _Digits));
                    
                    // 移动止损到保本位（入场价 + 小额利润保护）
                    double breakevenBuffer = _Point * 10;  // 10 点缓冲
                    double newSL = 0;
                    
                    if(positionType == POSITION_TYPE_BUY)
                        newSL = positionPrice + breakevenBuffer;
                    else
                        newSL = positionPrice - breakevenBuffer;
                    
                    newSL = NormalizeDouble(newSL, _Digits);
                    
                    // 修改止损
                    if(trade.PositionModify(ticket, newSL, positionTP))
                    {
                        Print("   新止损: ", DoubleToString(newSL, _Digits), " (保本位)");
                    }
                    else
                    {
                        Print("   ⚠️ 移动止损失败: ", trade.ResultRetcodeDescription());
                    }
                    Print("═══════════════════════════════════════════════════════════════");
                    
                    RemoveTP1Info(ticket);  // TP1 已触发，移除记录
                }
                else
                {
                    Print("❌ TP1 部分平仓失败 - 订单 ", ticket, ": ", trade.ResultRetcodeDescription());
                }
            }
        }
        
        //=================================================================
        // 追踪止损（可选：价格继续有利方向移动时）
        //=================================================================
        // 如果已经达到 TP1 且当前 R > 1.5R，可以继续追踪止损
        if(alreadyTP1 && currentRR > 1.5)
        {
            double trailingSL = 0;
            double trailBuffer = atr > 0 ? atr * 0.5 : risk * 0.3;
            
            if(positionType == POSITION_TYPE_BUY)
            {
                // 追踪止损 = 当前价 - 缓冲
                trailingSL = currentPrice - trailBuffer;
                trailingSL = NormalizeDouble(trailingSL, _Digits);
                
                // 只有新止损高于当前止损时才移动
                if(trailingSL > positionSL + _Point * 5)
                {
                    if(trade.PositionModify(ticket, trailingSL, positionTP))
                    {
                        Print("📈 追踪止损更新 - 订单 ", ticket, ": SL -> ", DoubleToString(trailingSL, _Digits), 
                              " (R: ", DoubleToString(currentRR, 2), ")");
                    }
                }
            }
            else
            {
                // 追踪止损 = 当前价 + 缓冲
                trailingSL = currentPrice + trailBuffer;
                trailingSL = NormalizeDouble(trailingSL, _Digits);
                
                // 只有新止损低于当前止损时才移动
                if(trailingSL < positionSL - _Point * 5)
                {
                    if(trade.PositionModify(ticket, trailingSL, positionTP))
                    {
                        Print("📉 追踪止损更新 - 订单 ", ticket, ": SL -> ", DoubleToString(trailingSL, _Digits),
                              " (R: ", DoubleToString(currentRR, 2), ")");
                    }
                }
            }
        }
    }
}

//+------------------------------------------------------------------+
//| Count Positions                                                   |
//+------------------------------------------------------------------+
int CountPositions()
{
    int count = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        if(!positionInfo.SelectByIndex(i)) continue;
        if(positionInfo.Magic() != InpMagicNumber) continue;
        if(positionInfo.Symbol() != _Symbol) continue;
        count++;
    }
    return count;
}

//+------------------------------------------------------------------+
//| Signal Type to String                                             |
//+------------------------------------------------------------------+
string SignalTypeToString(ENUM_SIGNAL_TYPE signal)
{
    switch(signal)
    {
        // Context Bypass 应急入场
        case SIGNAL_SPIKE_MARKET_BUY:  return "SpikeMarket_Buy";
        case SIGNAL_SPIKE_MARKET_SELL: return "SpikeMarket_Sell";
        case SIGNAL_EMERGENCY_SPIKE_BUY:  return "EmergencySpike_Buy";
        case SIGNAL_EMERGENCY_SPIKE_SELL: return "EmergencySpike_Sell";
        case SIGNAL_MICRO_CH_H1_BUY:   return "MicroCH_H1_Buy";
        case SIGNAL_MICRO_CH_H1_SELL:  return "MicroCH_H1_Sell";
        // 标准信号
        case SIGNAL_SPIKE_BUY:       return "Spike_Buy";
        case SIGNAL_SPIKE_SELL:      return "Spike_Sell";
        case SIGNAL_H1_BUY:          return "H1_Buy";
        case SIGNAL_H2_BUY:          return "H2_Buy";
        case SIGNAL_L1_SELL:         return "L1_Sell";
        case SIGNAL_L2_SELL:         return "L2_Sell";
        case SIGNAL_WEDGE_BUY:       return "Wedge_Buy";
        case SIGNAL_WEDGE_SELL:      return "Wedge_Sell";
        case SIGNAL_CLIMAX_BUY:      return "Climax_Buy";
        case SIGNAL_CLIMAX_SELL:     return "Climax_Sell";
        case SIGNAL_MTR_BUY:         return "MTR_Buy";
        case SIGNAL_MTR_SELL:        return "MTR_Sell";
        case SIGNAL_FAILED_BO_BUY:   return "FailedBO_Buy";
        case SIGNAL_FAILED_BO_SELL:  return "FailedBO_Sell";
        case SIGNAL_GAPBAR_BUY:      return "GapBar_Buy";
        case SIGNAL_GAPBAR_SELL:     return "GapBar_Sell";
        case SIGNAL_FINAL_FLAG_BUY:  return "FinalFlag_Buy";
        case SIGNAL_FINAL_FLAG_SELL: return "FinalFlag_Sell";
        default:                     return "Unknown";
    }
}

//+------------------------------------------------------------------+
