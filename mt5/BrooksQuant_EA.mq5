//+------------------------------------------------------------------+
//|                                                AlBrooks_v4.mq5   |
//|                          Al Brooks Price Action Trading System   |
//|                                                           v4     |
//+------------------------------------------------------------------+
#property copyright "BrooksQuant Team"
#property link      "https://github.com/brooksquant"
#property version   "4.11"
#property description "Al Brooks Price Action EA - MT5 Implementation"
#property description "Full PA Signals + Barb Wire Filter + Measuring Gap + Breakout Mode"
#property description "严格 NewBar 驱动: 除移动止损与价格监控(OnTickExitOnly)外, 所有计算均在 IsNewBar 为 true 时执行"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\OrderInfo.mqh>

//+------------------------------------------------------------------+
//| Enumerations                                                      |
//+------------------------------------------------------------------+
enum ENUM_MARKET_STATE
{
    MARKET_STATE_STRONG_TREND,
    MARKET_STATE_BREAKOUT,
    MARKET_STATE_CHANNEL,
    MARKET_STATE_TRADING_RANGE,
    MARKET_STATE_TIGHT_CHANNEL,
    MARKET_STATE_FINAL_FLAG
};

enum ENUM_MARKET_CYCLE
{
    MARKET_CYCLE_SPIKE,
    MARKET_CYCLE_CHANNEL,
    MARKET_CYCLE_TRADING_RANGE
};

// Always In 方向 - Brooks 最核心概念
enum ENUM_ALWAYS_IN
{
    AI_LONG,
    AI_SHORT,
    AI_NEUTRAL
};

enum ENUM_SIGNAL_TYPE
{
    SIGNAL_NONE,
    SIGNAL_SPIKE_BUY,
    SIGNAL_SPIKE_SELL,
    SIGNAL_H1_BUY,
    SIGNAL_H2_BUY,
    SIGNAL_L1_SELL,
    SIGNAL_L2_SELL,
    SIGNAL_MICRO_CH_BUY,
    SIGNAL_MICRO_CH_SELL,
    SIGNAL_DT_BUY,          // Double Bottom买
    SIGNAL_DT_SELL,          // Double Top卖
    SIGNAL_TREND_BAR_BUY,   // 趋势K线入场
    SIGNAL_TREND_BAR_SELL,
    SIGNAL_REV_BAR_BUY,     // 反转K线入场
    SIGNAL_REV_BAR_SELL,
    SIGNAL_II_BUY,           // ii/iii连续内包线
    SIGNAL_II_SELL,
    SIGNAL_OUTSIDE_BAR_BUY,  // 外包线反转
    SIGNAL_OUTSIDE_BAR_SELL,
    SIGNAL_MEASURED_MOVE_BUY,// 等距运动
    SIGNAL_MEASURED_MOVE_SELL,
    SIGNAL_TR_BREAKOUT_BUY,  // TR突破
    SIGNAL_TR_BREAKOUT_SELL,
    SIGNAL_BO_PULLBACK_BUY,  // 突破回调
    SIGNAL_BO_PULLBACK_SELL,
    SIGNAL_GAP_BAR_BUY,      // 缺口K线
    SIGNAL_GAP_BAR_SELL,
    SIGNAL_WEDGE_BUY,
    SIGNAL_WEDGE_SELL,
    SIGNAL_CLIMAX_BUY,
    SIGNAL_CLIMAX_SELL,
    SIGNAL_MTR_BUY,
    SIGNAL_MTR_SELL,
    SIGNAL_FAILED_BO_BUY,
    SIGNAL_FAILED_BO_SELL,
    SIGNAL_FINAL_FLAG_BUY,
    SIGNAL_FINAL_FLAG_SELL
};

//+------------------------------------------------------------------+
//| Input Parameters                                                  |
//+------------------------------------------------------------------+
input group "=== 基础设置 ==="
input double   InpLotSize           = 0.02;
input int      InpMagicNumber       = 20260203;
input int      InpMaxPositions      = 1;

input group "=== Al Brooks 核心参数 ==="
input int      InpEMAPeriod         = 20;
input int      InpATRPeriod         = 20;

input group "=== 信号开关 ==="
input bool     InpEnableSpike       = true;
input bool     InpEnableH2L2        = true;
input bool     InpEnableWedge       = true;
input bool     InpEnableClimax      = true;
input bool     InpEnableMTR         = true;
input bool     InpEnableFailedBO    = true;
input bool     InpEnableDTDB        = true;   // Double Top/Bottom
input bool     InpEnableTrendBar    = true;   // 趋势K线入场
input bool     InpEnableRevBar      = true;   // 反转K线入场
input bool     InpEnableIIPattern   = true;   // ii/iii连续内包线
input bool     InpEnableOutsideBar  = true;   // 外包线反转
input bool     InpEnableMeasuredMove= true;   // 等距运动
input bool     InpEnableTRBreakout  = true;   // TR突破
input bool     InpEnableBOPullback  = true;   // 突破回调
input bool     InpEnableGapBar      = true;   // 缺口K线

input group "=== 风险管理 ==="
input double   InpTP1ClosePercent   = 50.0;  // Scalp仓位占比(TP1=1:1),Runner占剩余
input double   InpMaxStopATRMult   = 3.0;    // 最大止损ATR倍数
input int      InpMaxSlippage       = 10;     // 最大滑点(点数),用于SetDeviationInPoints

input group "=== 高级设置 ==="
input bool     InpUseSignalBarSLInStrongTrend = true;  // 强趋势下取信号K线止损与结构止损的更紧者
input int      InpMinStopsLevelPoints = 30;   // 最小止步点数兜底(经纪商STOPS_LEVEL为0或过小时用此值,防invalid stops)
input bool     InpEnableHTFFilter   = true;
input bool     InpEnableWeekendFilter = true;  // 周末/周五尾盘过滤
input int      InpFridayCloseHour   = 22;     // 周五GMT≥此值禁止开新仓(22=收盘前约2h),0=仅六日
input int      InpMondayOpenHour    = 0;      // 周日GMT此点前禁止开新仓,0=周日全天禁
input double   InpFridayMinProfitR  = 1.5;   // 过周末门槛: 利润≥此倍数R且强趋势才保留,否则平
input double   InpMondayGapResetATR = 0.5;   // 周一跳空超此ATR倍数则重置H/L计数,0=不重置
input bool     InpEnableVerboseLog  = false;
input bool     InpDebugMode         = false;  // 为 true 时输出 Print 日志；回测时保持 false 可显著提速

input group "=== 止损与保本细化 ==="
input bool     InpTrailStopOnNewBarOnly = true;  // 移动止损仅在新K线收盘后评估(减少改单次数)
input int      InpSoftStopConfirmMode   = 0;     // 软止损确认: 0=收盘破 1=实体破 2=连续N根收破
input int      InpSoftStopConfirmBars   = 2;     // 连续收破根数(仅当确认模式=2时有效)
input double  InpBreakevenATRMult      = 0.1;   // 保本距离ATR倍数(0=用下方固定点数)
input int      InpBreakevenPoints       = 5;     // 保本固定点数(当ATR倍数=0时用)

//+------------------------------------------------------------------+
//| 内部常量                                                          |
//+------------------------------------------------------------------+
// 方向中性形态计算: 1=多 -1=空，用 (high-low)*direction 等统一逻辑
#define DIR_LONG   1
#define DIR_SHORT -1

const double   InpMinBodyRatio       = 0.50;
const double   InpClosePositionPct   = 0.25;
const int      InpLookbackPeriod     = 20;
const double   InpStrongTrendScore   = 0.50;
const int      InpSignalCooldown     = 3;

// Spike 参数
const int      InpMinSpikeBars       = 3;    // Brooks: Spike至少3根连续趋势K线
const double   InpSpikeOverlapMax    = 0.30; // Spike K线之间最大重叠比例

// Climax 反转
const double   InpSpikeClimaxATRMult = 3.0;
const bool     InpRequireSecondEntry = true;
const int      InpSecondEntryLookback= 10;

// 20 Gap Bar 法则
const bool     InpEnable20GapRule    = true;
const int      InpGapBarThreshold    = 20;
const bool     InpBlockFirstPullback = true;
const int      InpConsolidationBars  = 5;
const double   InpConsolidationRange = 1.5;

// HTF 过滤
const ENUM_TIMEFRAMES InpHTFTimeframe = PERIOD_H1;
const int      InpHTFEMAPeriod       = 20;

// 混合止损
const bool     InpEnableHardStop     = true;
const double   InpHardStopBufferMult = 1.5;
const bool     InpEnableSoftStop     = true;

// 点差过滤
const bool     InpEnableSpreadFilter  = true;
const double   InpMaxSpreadMult       = 2.0;
const int      InpSpreadLookback      = 20;

// 时段 (周末/周五尾盘过滤用)
const int      InpGMTOffset           = 0;

// Stop Order入场 - Brooks: 所有入场用Stop Order确认方向
const bool     InpUseStopOrders      = true;
const double   InpStopOrderOffset    = 0.0;

// Barb Wire 参数 - Brooks: 连续小K线/doji区域,避免交易
const bool     InpEnableBarbWireFilter = true;
const int      InpBarbWireMinBars    = 3;     // 至少3根小K线构成Barb Wire
const double   InpBarbWireBodyRatio  = 0.35;  // 小K线实体占比阈值
const double   InpBarbWireRangeRatio = 0.5;   // 小K线range相对ATR的阈值

// Measuring Gap 参数 - Brooks: 突破缺口,趋势中点标志
const bool     InpEnableMeasuringGap = true;
const double   InpMeasuringGapMinSize = 0.3;  // 最小缺口大小(ATR倍数)

// Breakout Mode 参数 - Brooks: 突破后特殊交易模式
const bool     InpEnableBreakoutMode = true;
const int      InpBreakoutModeBars   = 5;     // 突破模式持续K线数
const double   InpBreakoutModeATRMult = 1.5;  // 突破K线最小range(ATR倍数)

// ATR 动态阈值 - 替代固定百分比,适配 XAUUSD/EURUSD 等不同价位品种
const double   InpNearTrendlineATRMult = 0.2;  // 靠近趋势线/第三极值ATR倍数
const double   InpMinBufferATRMult     = 0.2;  // 最小缓冲ATR倍数(替代entryPrice*0.002)

// TTR (Tight Trading Range) - Brooks: 紧凑区间观望,避免反复吃耳光
const double   InpTTROverlapThreshold = 0.40;  // 重叠度阈值: 总范围/各棒range之和<此值视为TTR
const double   InpTTRRangeATRMult     = 2.5;   // TTR时区间宽度上限(ATR倍数)

// Swing Point / H-L Count - Brooks Push 定义
const int      InpSwingConfirmDepth   = 3;    // 确认波段点所需前后K线数(depth=1为临时)
const double   InpHLResetNewExtremeATR = 0.5;  // 显著新极值超越前波段ATR倍数时重置计数
const double   InpHLMinPullbackATR     = 0.2;  // 最小回调/反弹深度ATR倍数(Brooks Push)

//+------------------------------------------------------------------+
//| Global Variables                                                  |
//| 逻辑/状态变量均在 g_* 中，无独立 debug 全局变量；静态配置已用 const           |
//+------------------------------------------------------------------+
CTrade         trade;
CPositionInfo  positionInfo;

// 指标句柄：仅此三处，OnInit 创建 / GetMarketData 使用 / OnDeinit 释放，无未引用句柄；无 SendMail/SendNotification
int handleEMA;
int handleATR;
int handleHTFEMA;

double        g_HTFEMABuffer[];
string        g_HTFTrendDir = "";

ENUM_MARKET_STATE   g_MarketState      = MARKET_STATE_CHANNEL;
ENUM_MARKET_CYCLE   g_MarketCycle      = MARKET_CYCLE_CHANNEL;
ENUM_ALWAYS_IN      g_AlwaysIn         = AI_NEUTRAL;
string              g_TrendDirection   = "";
double              g_TrendStrength    = 0.0;
string              g_TightChannelDir  = "";

// Swing Point 追踪 - Brooks H/L计数基于swing point而非EMA
struct SwingPoint
{
    double price;
    int    barIndex;
    bool   isHigh;  // true=swing high, false=swing low
};
SwingPoint g_SwingPoints[];
int        g_SwingPointCount = 0;
#define MAX_SWING_POINTS 40

// 缓存常用swing point值(性能优化)
double     g_CachedSH1 = 0;  // 最近第1个swing high
double     g_CachedSH2 = 0;  // 最近第2个swing high
double     g_CachedSL1 = 0;  // 最近第1个swing low
double     g_CachedSL2 = 0;  // 最近第2个swing low

// 临时高低点 - 未达depth确认前的潜在波段点,用于止损以降低延迟
double     g_TempSwingHigh = 0;
double     g_TempSwingLow  = 0;
int        g_TempSwingHighBar = -1;
int        g_TempSwingLowBar  = -1;

// M5 波段点 - 用于 Runner 结构跟踪(仅当新 Higher Low 时上移止损)
#define MAX_M5_SWINGS 12
double     g_M5SwingLows[MAX_M5_SWINGS];
double     g_M5SwingHighs[MAX_M5_SWINGS];
int        g_M5SwingLowBars[MAX_M5_SWINGS];
int        g_M5SwingHighBars[MAX_M5_SWINGS];
int        g_M5SwingLowCount  = 0;
int        g_M5SwingHighCount = 0;

// H计数 (基于swing point)
int    g_H_Count           = 0;  // 当前H计数(H1,H2...)
double g_H_LastSwingHigh   = 0;  // 上一个swing high
double g_H_LastPullbackLow = 0;  // 上一个回调低点
int    g_H_LastPBLowBar    = -1;

// L计数 (基于swing point)
int    g_L_Count           = 0;
double g_L_LastSwingLow    = 0;
double g_L_LastBounceHigh  = 0;
int    g_L_LastBounceBar   = -1;

// 信号冷却期
datetime      g_LastBuySignalTime    = 0;
datetime      g_LastSellSignalTime   = 0;
int           g_LastBuySignalBar     = -999;
int           g_LastSellSignalBar    = -999;
double        g_LastBuyEntryPrice    = 0;
double        g_LastSellEntryPrice   = 0;

// Tight Channel 追踪
int           g_TightChannelBars     = 0;
double        g_TightChannelExtreme  = 0.0;
int           g_LastTightChannelEndBar = -1;

// Trading Range 边界
double        g_TR_High              = 0;
double        g_TR_Low               = 0;

// GapCount
int           g_GapCount             = 0;
double        g_GapCountExtreme      = 0.0;

// 20 Gap Bar 法则
bool          g_IsOverextended       = false;
bool          g_FirstPullbackBlocked = false;
string        g_OverextendDirection  = "";
datetime      g_OverextendStartTime  = 0;
bool          g_WaitingForRecovery   = false;
int           g_ConsolidationCount   = 0;
double        g_PullbackExtreme      = 0;
bool          g_FirstPullbackComplete= false;

// 状态惯性
ENUM_MARKET_STATE g_CurrentLockedState = MARKET_STATE_CHANNEL;
int           g_StateHoldBars        = 0;

int           g_BarCount             = 0;
datetime      g_LastBarTime          = 0;   // NewBar 判断用，避免 Tick 内重复计算与信号闪烁
datetime      g_LastRefreshRealTimeATR = 0;  // RefreshRealTimeATR 节流，避免 Tick 内频繁 CopyBuffer

// 点差追踪
double        g_SpreadHistory[];
int           g_SpreadIndex          = 0;
double        g_AverageSpread        = 0;
double        g_CurrentSpread        = 0;
bool          g_SpreadFilterActive   = false;
double        g_SpreadRunningSum     = 0;
int           g_SpreadValidCount     = 0;

// 时段检测
bool          g_IsWeekend             = false;  // 六日或周五尾盘: 禁止开新仓
bool          g_IsFridayClose        = false;  // 周五尾盘: 执行持仓管理
datetime      g_MondayGapResetDone    = 0;      // 本周一已做跳空重置的日期(避免重复)

// Barb Wire 追踪 - Brooks: 连续doji/小K线区域
bool          g_InBarbWire           = false;
int           g_BarbWireBarCount     = 0;
double        g_BarbWireHigh         = 0;
double        g_BarbWireLow          = 0;

// Measuring Gap 追踪 - Brooks: 突破缺口标记趋势中点
struct MeasuringGapInfo
{
    double gapHigh;
    double gapLow;
    string direction;  // "up" or "down"
    int    barIndex;
    bool   isValid;
};
MeasuringGapInfo g_MeasuringGap;
bool          g_HasMeasuringGap      = false;

// Breakout Mode 追踪 - Brooks: 突破后特殊交易模式
bool          g_InBreakoutMode       = false;
string        g_BreakoutModeDir      = "";
int           g_BreakoutModeBarCount = 0;
double        g_BreakoutModeEntry    = 0;
double        g_BreakoutModeExtreme  = 0;

// 品种信息
int           g_SymbolDigits         = 0;
double        g_SymbolPoint          = 0;
double        g_SymbolTickSize       = 0;

// 混合止损
struct SoftStopInfo
{
    ulong  ticket;
    double technicalSL;
    string side;
    double tp1Price;   // Runner用: TP1触及后移保本; 0=Scalp/不启用
};
SoftStopInfo g_SoftStopList[];
int          g_SoftStopCount = 0;

// TP1 追踪
struct TP1Info
{
    ulong  ticket;
    double tp1Price;
    string side;
};
TP1Info g_TP1List[];
int     g_TP1Count = 0;
#define MAX_TP1_RECORDS 32

// 反转尝试跟踪
struct ReversalAttempt
{
    datetime time;
    double   price;
    string   direction;
    bool     failed;
};
ReversalAttempt g_LastReversalAttempt;
bool            g_HasPendingReversal = false;
int             g_ReversalAttemptCount = 0;

// 趋势线追踪 (MTR用)
double        g_TrendLineStart     = 0;
double        g_TrendLineEnd       = 0;
int           g_TrendLineStartBar  = 0;
int           g_TrendLineEndBar    = 0;
bool          g_TrendLineBroken    = false;
double        g_TrendLineBreakPrice= 0;

// Breakout Pullback追踪
bool          g_RecentBreakout     = false;
string        g_BreakoutDir        = "";
double        g_BreakoutLevel      = 0;
int           g_BreakoutBarAge     = 0;

// 缓存数组
double        g_EMABuffer[];
double        g_ATRBuffer[];
double        g_CloseBuffer[];
double        g_OpenBuffer[];
double        g_HighBuffer[];
double        g_LowBuffer[];
long          g_VolumeBuffer[];
int           g_BufferSize = 0;
double        g_AtrValue   = 0;   // 实时波动率参考(异常波幅时刷新,供 CheckSoftStopExit 防扫单)

// STOP单待处理信息（orderComment 两笔单时为 Brooks_Scalp / Brooks_Runner）
struct PendingStopOrderInfo
{
    ulong  orderTicket;
    double technicalSL;
    double tp1Price;
    string side;
    string signalName;
    string orderComment;
};
PendingStopOrderInfo g_PendingStopOrders[];
int g_PendingStopOrderCount = 0;
#define MAX_PENDING_STOP_ORDERS 16

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
    trade.SetExpertMagicNumber(InpMagicNumber);
    trade.SetDeviationInPoints((ulong)MathMax(1, InpMaxSlippage));
    
    long fillMode = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
    if((fillMode & SYMBOL_FILLING_IOC) != 0)
        trade.SetTypeFilling(ORDER_FILLING_IOC);
    else if((fillMode & SYMBOL_FILLING_FOK) != 0)
        trade.SetTypeFilling(ORDER_FILLING_FOK);
    else
        trade.SetTypeFilling(ORDER_FILLING_RETURN);
    
    handleEMA = iMA(_Symbol, PERIOD_CURRENT, InpEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
    handleATR = iATR(_Symbol, PERIOD_CURRENT, InpATRPeriod);
    handleHTFEMA = iMA(_Symbol, InpHTFTimeframe, InpHTFEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
    
    if(handleEMA == INVALID_HANDLE || handleATR == INVALID_HANDLE || handleHTFEMA == INVALID_HANDLE)
    {
        if(InpDebugMode) Print("指标初始化失败");
        return INIT_FAILED;
    }
    
    ArraySetAsSeries(g_EMABuffer, true);
    ArraySetAsSeries(g_ATRBuffer, true);
    ArraySetAsSeries(g_HTFEMABuffer, true);
    ArraySetAsSeries(g_CloseBuffer, true);
    ArraySetAsSeries(g_OpenBuffer, true);
    ArraySetAsSeries(g_HighBuffer, true);
    ArraySetAsSeries(g_LowBuffer, true);
    ArraySetAsSeries(g_VolumeBuffer, true);
    
    g_SymbolDigits   = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
    g_SymbolPoint    = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    g_SymbolTickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    
    ArrayResize(g_SpreadHistory, InpSpreadLookback);
    ArrayInitialize(g_SpreadHistory, 0);
    ArrayResize(g_SwingPoints, MAX_SWING_POINTS);
    
    // 初始化新增Brooks概念的全局变量
    g_InBarbWire = false;
    g_BarbWireBarCount = 0;
    g_BarbWireHigh = 0;
    g_BarbWireLow = 0;
    
    g_HasMeasuringGap = false;
    g_MeasuringGap.gapHigh = 0;
    g_MeasuringGap.gapLow = 0;
    g_MeasuringGap.direction = "";
    g_MeasuringGap.barIndex = 0;
    g_MeasuringGap.isValid = false;
    
    g_InBreakoutMode = false;
    g_BreakoutModeDir = "";
    g_BreakoutModeBarCount = 0;
    g_BreakoutModeEntry = 0;
    g_BreakoutModeExtreme = 0;
    
    // 初始化缓存的swing point值及临时高低点
    g_CachedSH1 = 0; g_CachedSH2 = 0;
    g_CachedSL1 = 0; g_CachedSL2 = 0;
    g_TempSwingHigh = 0; g_TempSwingLow = 0;
    g_TempSwingHighBar = -1; g_TempSwingLowBar = -1;
    
    // 检查本 EA 已有仓位（Scalp + Runner 双 Magic）
    int existingCount = CountPositions();
    if(existingCount > 0 && InpDebugMode)
        Print("已有仓位: ", existingCount, " (Magic ", InpMagicNumber, " Scalp / ", InpMagicNumber + 1, " Runner)");
    
    Print("AlBrooks_v4 启动 | 当前时间:", TimeToString(TimeCurrent(), TIME_DATE|TIME_MINUTES|TIME_SECONDS), " | 编译:", TimeToString(__DATETIME__, TIME_DATE|TIME_MINUTES), " | ", _Symbol, " ", EnumToString(Period()));
    if(InpDebugMode) Print("AlBrooks_v4 初始化成功 | Magic ", InpMagicNumber);
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    if(handleEMA != INVALID_HANDLE)    IndicatorRelease(handleEMA);
    if(handleATR != INVALID_HANDLE)    IndicatorRelease(handleATR);
    if(handleHTFEMA != INVALID_HANDLE) IndicatorRelease(handleHTFEMA);
    ObjectsDeleteAll(0, "BQ_");
}

//+------------------------------------------------------------------+
//| NewBar 判断 - 仅在新 K 线收盘后执行重逻辑，避免 Tick 内重复计算与信号闪烁   |
//+------------------------------------------------------------------+
bool IsNewBar()
{
    datetime currTime = iTime(_Symbol, PERIOD_CURRENT, 0);
    if(currTime != g_LastBarTime)
    {
        g_LastBarTime = currTime;
        return true;
    }
    return false;
}

//+------------------------------------------------------------------+
//| 非 NewBar 时仅用当前价触发平仓，不拷贝历史/不重算指标                  |
//+------------------------------------------------------------------+
void OnTickExitOnly(double bid, double ask)
{
    if(!InpEnableSoftStop || g_SoftStopCount == 0) { CheckPendingStopOrderFills(); return; }
    ValidateSoftStopArray();
    if(g_SoftStopCount == 0) { CheckPendingStopOrderFills(); return; }
    static int syncCounter = 0;
    if(++syncCounter >= 10) { SyncSoftStopList(); syncCounter = 0; }
    ValidateSoftStopArray();
    if(g_SoftStopCount == 0) { CheckPendingStopOrderFills(); return; }
    for(int i = g_SoftStopCount - 1; i >= 0; i--)
    {
        if(i < 0 || i >= g_SoftStopCount || i >= ArraySize(g_SoftStopList)) break;
        ulong ticket = g_SoftStopList[i].ticket;
        double techSL = g_SoftStopList[i].technicalSL;
        string side   = g_SoftStopList[i].side;
        if(!PositionSelectByTicket(ticket)) { RemoveSoftStopInfo(ticket); continue; }
        long posMagic = PositionGetInteger(POSITION_MAGIC);
        if(posMagic != InpMagicNumber && posMagic != InpMagicNumber + 1) { RemoveSoftStopInfo(ticket); continue; }
        bool shouldClose = (side == "buy" && bid < techSL) || (side == "sell" && ask > techSL);
        if(shouldClose && PositionCloseWithRetry(ticket))
        {
            if(InpDebugMode) Print("逻辑止损触发(Tick) #", ticket, " 技术SL:", DoubleToString(techSL, g_SymbolDigits));
            RemoveSoftStopInfo(ticket);
        }
    }
    CheckPendingStopOrderFills();
}

//+------------------------------------------------------------------+
//| 合并方向逻辑：按 direction 扫描市场，返回该方向的第一个有效信号           |
//| direction: DIR_LONG=多 DIR_SHORT=空；除 BreakoutMode 外所有信号经此入口   |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE ScanMarket(int direction, double ema, double atr, double &stopLoss, double &baseHeight)
{
    const string wantSide = (direction == DIR_LONG) ? "buy" : "sell";
    ENUM_SIGNAL_TYPE s = SIGNAL_NONE;
    bool inTTR = IsTightTradingRange(atr);  // TTR时过滤趋势类信号,避免震荡市吃耳光

    if(!inTTR && InpEnableSpike) { s = CheckSpike(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(!inTTR) { s = CheckMicroChannel(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(InpEnableH2L2) { s = CheckHLCountSignal(direction, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE) return s; }
    if(!inTTR && InpEnableBOPullback) { s = CheckBreakoutPullback(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(!inTTR && InpEnableTrendBar) { s = CheckTrendBarEntry(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(!inTTR && InpEnableGapBar) { s = CheckGapBar(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(InpEnableTRBreakout && g_MarketState == MARKET_STATE_TRADING_RANGE) { s = CheckTRBreakout(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }

    bool allowReversal = (g_MarketState == MARKET_STATE_TRADING_RANGE || g_MarketState == MARKET_STATE_FINAL_FLAG || g_MarketCycle == MARKET_CYCLE_SPIKE);
    if(InpEnableClimax) { s = CheckClimax(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(InpEnableWedge && allowReversal) { s = CheckWedgeDirection(direction, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE) return s; }
    if(InpEnableMTR && allowReversal) { s = CheckMTR(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(InpEnableFailedBO && g_MarketState == MARKET_STATE_TRADING_RANGE) { s = CheckFailedBreakout(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(InpEnableDTDB && allowReversal) { s = CheckDoubleTopBottomDirection(direction, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE) return s; }
    if(InpEnableOutsideBar && allowReversal) { s = CheckOutsideBarReversal(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(InpEnableRevBar && allowReversal) { s = CheckReversalBarEntry(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(InpEnableIIPattern && allowReversal) { s = CheckIIPattern(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(InpEnableMeasuredMove) { s = CheckMeasuredMove(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }
    if(g_MarketState == MARKET_STATE_FINAL_FLAG) { s = CheckFinalFlag(ema, atr, stopLoss, baseHeight); if(s != SIGNAL_NONE && GetSignalSide(s) == wantSide) return s; }

    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
    bool isNewBar = IsNewBar();

    int posCount = CountPositions();

    if(!isNewBar)
    {
        if(posCount > 0 && g_AtrValue > 0)
        {
            double currentRange = iHigh(_Symbol, PERIOD_CURRENT, 0) - iLow(_Symbol, PERIOD_CURRENT, 0);
            if(currentRange > g_AtrValue * 1.5)
                RefreshRealTimeATR();
        }
        double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
        double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        OnTickExitOnly(bid, ask);
        return;
    }

    if(!GetMarketData())
        return;

    g_BarCount++;
    double ema = g_EMABuffer[1];
    double atr = g_ATRBuffer[1];
    if(ema == 0 || atr == 0)
        return;

    g_AtrValue = atr;
    if(posCount > 0 && g_BufferSize >= 2)
    {
        double bar1Range = g_HighBuffer[1] - g_LowBuffer[1];
        if(bar1Range > atr * 1.5)
            RefreshRealTimeATR();
    }

    double atrForSL = (g_AtrValue > 0) ? g_AtrValue : atr;
    AdoptExistingPositionsIfNeeded(atrForSL);

    if(posCount > 0) UpdateM5SwingPoints();
    CheckSoftStopExit();
    CheckPendingStopOrderFills();
    CheckAndCancelExpiredOrders();  // Brooks 信号棒-入场棒 时效: 清理未触发的挂单
    UpdateSpreadTracking();
    UpdateSessionDetection();

    // Swing 点、市场状态、H/L 计数、周一跳空 — 仅在收盘后更新
    UpdateSwingPoints(atr);
    if(InpEnableWeekendFilter)
        CheckMondayGapReset(atr);
    UpdateAlwaysInDirection(ema, atr);
    if(g_LastTightChannelEndBar >= 0)
        g_LastTightChannelEndBar++;
    DetectMarketState(ema, atr);
    g_MarketCycle = GetMarketCycle(g_MarketState);
    UpdateHLCount(atr);
    int gapCount = CalculateGapCount(ema, atr);
    Update20GapBarRule(ema, atr);
    UpdateReversalAttemptTracking();
    UpdateTrendLine(atr);
    UpdateBarbWireDetection(atr);
    UpdateMeasuringGap(ema, atr);
    UpdateBreakoutMode(ema, atr);
    PrintBarLog(gapCount, "");

    // Barb Wire 过滤
    if(InpEnableBarbWireFilter && g_InBarbWire)
    {
        if(InpEnableVerboseLog)
            if(InpDebugMode) Print("Barb Wire活跃,跳过信号检测");
        ManagePositions(ema, atr);
        return;
    }

    // 3. 信号检测 (Al Brooks 风格：K 线收盘决断，使用上面 NewBar 计算出的缓存值；除移动止损与价格监控外均仅在 NewBar 内)
    ENUM_SIGNAL_TYPE signal = SIGNAL_NONE;
    double stopLoss = 0;
    double baseHeight = 0;
    UpdateBreakoutPullbackTracking(ema, atr);

    if(g_InBreakoutMode)
    {
        signal = CheckBreakoutModeSignal(ema, atr, stopLoss, baseHeight);
        if(signal != SIGNAL_NONE)
        {
            if(stopLoss > 0)
                ProcessSignal(signal, stopLoss, baseHeight);
            ManagePositions(ema, atr);
            return;
        }
    }

    signal = ScanMarket(DIR_LONG, ema, atr, stopLoss, baseHeight);
    if(signal == SIGNAL_NONE)
        signal = ScanMarket(DIR_SHORT, ema, atr, stopLoss, baseHeight);

    if(signal != SIGNAL_NONE)
        PrintSignalLog(signal, stopLoss, atr);
    if(signal != SIGNAL_NONE && stopLoss > 0 && (!InpEnableWeekendFilter || !g_IsWeekend))
        ProcessSignal(signal, stopLoss, baseHeight);

    // 4. 订单/持仓管理 (移动止损等，新 K 线做一次，减少 broker 交互)
    ManagePositions(ema, atr);
}


//+------------------------------------------------------------------+
//| Always In Direction - Brooks核心: 任何时刻市场有一个AI方向          |
//| 优先级: 两棒突破通道 > 极强趋势棒突破结构 > 强力反转 > 评分(收盘靠极值+)  |
//+------------------------------------------------------------------+
void UpdateAlwaysInDirection(double ema, double atr)
{
    if(g_BufferSize < 20 || atr <= 0) { g_AlwaysIn = AI_NEUTRAL; return; }
    
    double lastBody = g_CloseBuffer[1] - g_OpenBuffer[1];
    double lastRange = g_HighBuffer[1] - g_LowBuffer[1];
    double closePos = (lastRange > 0) ? (g_CloseBuffer[1] - g_LowBuffer[1]) / lastRange : 0.5;
    double bodyRatio = (lastRange > 0) ? MathAbs(lastBody) / lastRange : 0;
    
    // --- 最高优先级: 两棒确认 - 连续两根反向趋势棒突破通道线(EMA) ---
    if(g_BufferSize >= 4)
    {
        double body1 = g_CloseBuffer[1] - g_OpenBuffer[1];
        double body2 = g_CloseBuffer[2] - g_OpenBuffer[2];
        double range1 = g_HighBuffer[1] - g_LowBuffer[1];
        double range2 = g_HighBuffer[2] - g_LowBuffer[2];
        double ema2 = (ArraySize(g_EMABuffer) > 2) ? g_EMABuffer[2] : ema;
        bool bar1Bull = (range1 > 0 && body1 / range1 > 0.55);
        bool bar1Bear = (range1 > 0 && body1 / range1 < -0.55);
        bool bar2Bull = (range2 > 0 && body2 / range2 > 0.55);
        bool bar2Bear = (range2 > 0 && body2 / range2 < -0.55);
        if(bar1Bull && bar2Bull && g_CloseBuffer[1] > ema && g_CloseBuffer[2] > ema2)
            { g_AlwaysIn = AI_LONG; return; }
        if(bar1Bear && bar2Bear && g_CloseBuffer[1] < ema && g_CloseBuffer[2] < ema2)
            { g_AlwaysIn = AI_SHORT; return; }
    }
    
    // --- 强力反转优先: 极强趋势棒(实体>前3根均长2倍)+突破EMA/结构位+收盘靠极值 ---
    if(g_BufferSize >= 5 && lastRange > atr * 1.0)
    {
        double avgBody3 = 0;
        for(int k = 2; k <= 4 && k < g_BufferSize; k++)
            avgBody3 += MathAbs(g_CloseBuffer[k] - g_OpenBuffer[k]);
        avgBody3 /= 3.0;
        double bodyLen = MathAbs(lastBody);
        bool breakEMA = (lastBody > 0 && g_CloseBuffer[1] > ema) || (lastBody < 0 && g_CloseBuffer[1] < ema);
        bool breakStruct = false;
        if(g_SwingPointCount >= 2)
        {
            double sh1 = GetRecentSwingHigh(1), sl1 = GetRecentSwingLow(1);
            if(lastBody > 0 && sh1 > 0 && g_CloseBuffer[1] > sh1) breakStruct = true;
            if(lastBody < 0 && sl1 > 0 && g_CloseBuffer[1] < sl1) breakStruct = true;
        }
        if(avgBody3 > 0 && bodyLen > avgBody3 * 2.0 && bodyRatio > 0.6 && (breakEMA || breakStruct))
        {
            if(lastBody > 0 && closePos > 0.75) { g_AlwaysIn = AI_LONG; return; }
            if(lastBody < 0 && closePos < 0.25) { g_AlwaysIn = AI_SHORT; return; }
        }
    }
    
    // --- 直接翻转: 强力反转K线(大实体+收盘靠极值) ---
    if(lastRange > atr * 1.2 && bodyRatio > 0.65)
    {
        if(lastBody > 0 && closePos > 0.75) { g_AlwaysIn = AI_LONG; return; }
        if(lastBody < 0 && closePos < 0.25) { g_AlwaysIn = AI_SHORT; return; }
    }
    
    // --- 评分制: 降低微小Overlap权重, 提升收盘靠极值权重 ---
    int bullCount = 0, bearCount = 0;
    int overlapPenalty = 0;
    for(int i = 1; i <= 5 && i < g_BufferSize; i++)
    {
        double body = g_CloseBuffer[i] - g_OpenBuffer[i];
        double range = g_HighBuffer[i] - g_LowBuffer[i];
        if(range <= 0) continue;
        double br = MathAbs(body) / range;
        bool hasOverlap = false;
        if(i < g_BufferSize - 1)
        {
            double ovHigh = MathMin(g_HighBuffer[i], g_HighBuffer[i+1]);
            double ovLow  = MathMax(g_LowBuffer[i], g_LowBuffer[i+1]);
            if(ovHigh > ovLow && range > 0 && (ovHigh - ovLow) / range > 0.6) hasOverlap = true;
        }
        if(body > 0 && br > 0.5) { bullCount++; if(hasOverlap) overlapPenalty++; }
        if(body < 0 && br > 0.5) { bearCount++; if(hasOverlap) overlapPenalty++; }
    }
    
    int hh = 0, hl = 0, lh = 0, ll = 0;
    for(int i = 1; i < g_SwingPointCount - 1 && i < 4; i++)
    {
        int j = i + 1;
        if(j >= g_SwingPointCount) break;
        if(g_SwingPoints[i].isHigh && g_SwingPoints[j].isHigh)
        {
            if(g_SwingPoints[i].price > g_SwingPoints[j].price) hh++;
            else lh++;
        }
        if(!g_SwingPoints[i].isHigh && !g_SwingPoints[j].isHigh)
        {
            if(g_SwingPoints[i].price > g_SwingPoints[j].price) hl++;
            else ll++;
        }
    }
    
    bool aboveEMA = g_CloseBuffer[1] > ema;
    double bullScore = 0, bearScore = 0;
    
    double countWeight = (overlapPenalty >= 2) ? 0.25 : ((overlapPenalty >= 1) ? 0.35 : 0.4);
    if(bullCount >= 3) bullScore += countWeight;
    else if(bullCount >= 2) bullScore += countWeight * 0.5;
    if(bearCount >= 3) bearScore += countWeight;
    else if(bearCount >= 2) bearScore += countWeight * 0.5;
    
    if(hh > 0 && hl > 0) bullScore += 0.30;
    if(lh > 0 && ll > 0) bearScore += 0.30;
    
    if(aboveEMA) bullScore += 0.12;
    else bearScore += 0.12;
    
    if(lastRange > 0 && lastRange > atr * 1.5)
    {
        if(lastBody > 0) bullScore += (bodyRatio > 0.7 ? 0.35 : 0.25);
        else bearScore += (bodyRatio > 0.7 ? 0.35 : 0.25);
    }
    
    if(closePos > 0.8) bullScore += 0.20;
    if(closePos < 0.2) bearScore += 0.20;
    
    if(bullScore >= 0.5 && bullScore > bearScore + 0.1)
        g_AlwaysIn = AI_LONG;
    else if(bearScore >= 0.5 && bearScore > bullScore + 0.1)
        g_AlwaysIn = AI_SHORT;
    else
        g_AlwaysIn = AI_NEUTRAL;
}

//+------------------------------------------------------------------+
//| Swing Point Detection - Brooks H/L计数的基础                      |
//| 确认波段(depth=3): 用于H/L计数、形态识别; 临时波段(depth=1): 用于止损,降低延迟 |
//+------------------------------------------------------------------+
void UpdateSwingPoints(double atr)
{
    // atr 保留以与调用方签名一致,当前逻辑未使用
    // 先递增所有现有swing的bar索引(因为新K线产生了)
    for(int i = 0; i < g_SwingPointCount; i++)
        g_SwingPoints[i].barIndex++;
    
    // 清理过老的swing points (超过40根K线)
    while(g_SwingPointCount > 0 && g_SwingPoints[g_SwingPointCount - 1].barIndex > 40)
        g_SwingPointCount--;
    
    int depth = InpSwingConfirmDepth;
    int checkBar = depth + 1;
    
    // --- 临时高低点(depth=1): 仅需前后各1根确认,用于止损,约1根K线延迟 ---
    if(g_BufferSize >= 4)
    {
        int tempBar = 2;  // bar[2]=1根前,左右各1根
        if(g_HighBuffer[1] < g_HighBuffer[tempBar] && g_HighBuffer[3] < g_HighBuffer[tempBar])
        {
            g_TempSwingHigh = g_HighBuffer[tempBar];
            g_TempSwingHighBar = tempBar;
        }
        if(g_LowBuffer[1] > g_LowBuffer[tempBar] && g_LowBuffer[3] > g_LowBuffer[tempBar])
        {
            g_TempSwingLow = g_LowBuffer[tempBar];
            g_TempSwingLowBar = tempBar;
        }
    }
    
    // --- 确认波段(depth=3): 用于H/L计数、形态、趋势线 ---
    if(g_BufferSize < checkBar + depth + 1) return;
    
    bool isSwingHigh = true;
    double centerHigh = g_HighBuffer[checkBar];
    for(int i = 1; i <= depth; i++)
    {
        int leftIdx = checkBar - i;
        int rightIdx = checkBar + i;
        if(leftIdx < 0 || rightIdx >= g_BufferSize) { isSwingHigh = false; break; }
        if(g_HighBuffer[leftIdx] >= centerHigh) { isSwingHigh = false; break; }
        if(g_HighBuffer[rightIdx] >= centerHigh) { isSwingHigh = false; break; }
    }
    
    bool isSwingLow = true;
    double centerLow = g_LowBuffer[checkBar];
    for(int i = 1; i <= depth; i++)
    {
        int leftIdx = checkBar - i;
        int rightIdx = checkBar + i;
        if(leftIdx < 0 || rightIdx >= g_BufferSize) { isSwingLow = false; break; }
        if(g_LowBuffer[leftIdx] <= centerLow) { isSwingLow = false; break; }
        if(g_LowBuffer[rightIdx] <= centerLow) { isSwingLow = false; break; }
    }
    
    if(isSwingHigh)
        AddSwingPoint(centerHigh, checkBar, true);
    if(isSwingLow)
        AddSwingPoint(centerLow, checkBar, false);
}

void AddSwingPoint(double price, int barIndex, bool isHigh)
{
    // 避免重复: 同一bar不重复添加同类型swing
    for(int i = 0; i < g_SwingPointCount; i++)
    {
        if(g_SwingPoints[i].barIndex == barIndex && g_SwingPoints[i].isHigh == isHigh)
            return;
    }
    
    // 插入到最前面(最新的在前)
    if(g_SwingPointCount >= MAX_SWING_POINTS)
        g_SwingPointCount = MAX_SWING_POINTS - 1;
    
    for(int i = g_SwingPointCount; i > 0; i--)
        g_SwingPoints[i] = g_SwingPoints[i-1];
    
    g_SwingPoints[0].price = price;
    g_SwingPoints[0].barIndex = barIndex;
    g_SwingPoints[0].isHigh = isHigh;
    g_SwingPointCount++;
    
    // 更新缓存的swing point值
    UpdateCachedSwingPoints();
}

// 更新缓存的swing point值(性能优化)
void UpdateCachedSwingPoints()
{
    g_CachedSH1 = 0; g_CachedSH2 = 0;
    g_CachedSL1 = 0; g_CachedSL2 = 0;
    
    int shCount = 0, slCount = 0;
    for(int i = 0; i < g_SwingPointCount && (shCount < 2 || slCount < 2); i++)
    {
        if(g_SwingPoints[i].isHigh && shCount < 2)
        {
            if(shCount == 0) g_CachedSH1 = g_SwingPoints[i].price;
            else g_CachedSH2 = g_SwingPoints[i].price;
            shCount++;
        }
        else if(!g_SwingPoints[i].isHigh && slCount < 2)
        {
            if(slCount == 0) g_CachedSL1 = g_SwingPoints[i].price;
            else g_CachedSL2 = g_SwingPoints[i].price;
            slCount++;
        }
    }
}

// M5 波段点更新 - Runner 结构跟踪用; 仅在新 K 线时调用
void UpdateM5SwingPoints()
{
    const int depth = 3, needBars = depth * 2 + 5;
    double m5High[], m5Low[];
    ArraySetAsSeries(m5High, true);
    ArraySetAsSeries(m5Low, true);
    if(CopyHigh(_Symbol, PERIOD_M5, 0, needBars, m5High) < needBars || CopyLow(_Symbol, PERIOD_M5, 0, needBars, m5Low) < needBars)
        return;
    
    double tmpLows[MAX_M5_SWINGS], tmpHighs[MAX_M5_SWINGS];
    int tmpLowBars[MAX_M5_SWINGS], tmpHighBars[MAX_M5_SWINGS];
    int nLow = 0, nHigh = 0;
    for(int checkBar = depth + 1; checkBar < needBars - depth - 1 && (nLow < MAX_M5_SWINGS || nHigh < MAX_M5_SWINGS); checkBar++)
    {
        bool isSL = true;
        double centerLow = m5Low[checkBar];
        for(int i = 1; i <= depth; i++)
        {
            if(m5Low[checkBar - i] <= centerLow || m5Low[checkBar + i] <= centerLow)
            { isSL = false; break; }
        }
        if(isSL && nLow < MAX_M5_SWINGS)
        { tmpLows[nLow] = centerLow; tmpLowBars[nLow] = checkBar; nLow++; }
        
        bool isSH = true;
        double centerHigh = m5High[checkBar];
        for(int i = 1; i <= depth; i++)
        {
            if(m5High[checkBar - i] >= centerHigh || m5High[checkBar + i] >= centerHigh)
            { isSH = false; break; }
        }
        if(isSH && nHigh < MAX_M5_SWINGS)
        { tmpHighs[nHigh] = centerHigh; tmpHighBars[nHigh] = checkBar; nHigh++; }
    }
    // 按 bar 索引升序(最 recent 在前)
    for(int i = 0; i < nLow; i++)
    {
        int best = i;
        for(int j = i + 1; j < nLow; j++)
            if(tmpLowBars[j] < tmpLowBars[best]) best = j;
        if(best != i)
        {
            double dp = tmpLows[i]; tmpLows[i] = tmpLows[best]; tmpLows[best] = dp;
            int di = tmpLowBars[i]; tmpLowBars[i] = tmpLowBars[best]; tmpLowBars[best] = di;
        }
        g_M5SwingLows[i] = tmpLows[i];
        g_M5SwingLowBars[i] = tmpLowBars[i];
    }
    g_M5SwingLowCount = nLow;
    for(int i = 0; i < nHigh; i++)
    {
        int best = i;
        for(int j = i + 1; j < nHigh; j++)
            if(tmpHighBars[j] < tmpHighBars[best]) best = j;
        if(best != i)
        {
            double dp = tmpHighs[i]; tmpHighs[i] = tmpHighs[best]; tmpHighs[best] = dp;
            int di = tmpHighBars[i]; tmpHighBars[i] = tmpHighBars[best]; tmpHighBars[best] = di;
        }
        g_M5SwingHighs[i] = tmpHighs[i];
        g_M5SwingHighBars[i] = tmpHighBars[i];
    }
    g_M5SwingHighCount = nHigh;
}

// 结构跟踪: 仅当 M5 形成新 Higher Low(买) 且高于开仓价时，返回新止损位；否则 0
double GetM5StructuralStopForBuy(double entryPrice, double currentSL, double atr)
{
    if(g_M5SwingLowCount < 2 || atr <= 0) return 0;
    double buf = atr * 0.2;
    for(int i = 0; i < g_M5SwingLowCount - 1; i++)
    {
        double newLow = g_M5SwingLows[i];
        double prevLow = g_M5SwingLows[i + 1];
        if(newLow > entryPrice && newLow > prevLow && (currentSL <= 0 || newLow > currentSL + buf))
            return NormalizeDouble(newLow - buf, g_SymbolDigits);
    }
    return 0;
}

// 结构跟踪: 仅当 M5 形成新 Lower High(卖) 且低于开仓价时，返回新止损位；否则 0
double GetM5StructuralStopForSell(double entryPrice, double currentSL, double atr)
{
    if(g_M5SwingHighCount < 2 || atr <= 0) return 0;
    double buf = atr * 0.2;
    for(int i = 0; i < g_M5SwingHighCount - 1; i++)
    {
        double newHigh = g_M5SwingHighs[i];
        double prevHigh = g_M5SwingHighs[i + 1];
        if(newHigh < entryPrice && newHigh < prevHigh && (currentSL <= 0 || newHigh < currentSL - buf))
            return NormalizeDouble(newHigh + buf, g_SymbolDigits);
    }
    return 0;
}

// 高潮退出: 检测 Climax Bar(实体>前5根均值3倍 且 触及通道轨)，市价全平 Runner
void CheckClimaxExit()  
{
    if(g_BufferSize < 7 || g_MarketState != MARKET_STATE_TIGHT_CHANNEL || g_TightChannelExtreme <= 0) return;
    
    double currBody = MathAbs(g_CloseBuffer[1] - g_OpenBuffer[1]);
    double avgBody = 0;
    for(int i = 2; i <= 6 && i < g_BufferSize; i++)
        avgBody += MathAbs(g_CloseBuffer[i] - g_OpenBuffer[i]);
    avgBody /= 5.0;
    if(avgBody <= 0 || currBody < avgBody * 3.0) return;
    
    double tol = g_SymbolPoint * 10;
    bool buyClimax  = (g_CloseBuffer[1] > g_OpenBuffer[1]) && (g_TightChannelDir == "up") && (g_HighBuffer[1] >= g_TightChannelExtreme - tol);
    bool sellClimax = (g_CloseBuffer[1] < g_OpenBuffer[1]) && (g_TightChannelDir == "down") && (g_LowBuffer[1] <= g_TightChannelExtreme + tol);
    
    if(!buyClimax && !sellClimax) return;
    
    long magicRunner = InpMagicNumber + 1;
    for(int p = PositionsTotal() - 1; p >= 0; p--)
    {
        if(!positionInfo.SelectByIndex(p)) continue;
        if(positionInfo.Symbol() != _Symbol || positionInfo.Magic() != magicRunner) continue;
        
        ulong ticket = positionInfo.Ticket();
        bool isBuy = (positionInfo.PositionType() == POSITION_TYPE_BUY);
        if((buyClimax && isBuy) || (sellClimax && !isBuy))
        {
            if(PositionCloseWithRetry(ticket))
            {
                RemoveSoftStopInfo(ticket);
                if(InpDebugMode) Print("高潮退出: Climax Bar 检测 #", ticket, " 市价全平");
            }
        }
    }
}

// Scalp TP1: 1:1 盈亏比. TP1 = Entry + (Entry - SL) = Entry + Risk
double GetScalpTP1(const string &side, double entryPrice, double initialSL)
{
    double risk = (side == "buy") ? (entryPrice - initialSL) : (initialSL - entryPrice);
    if(risk <= 0) return 0;
    if(side == "buy") return NormalizeDouble(entryPrice + risk, g_SymbolDigits);
    return NormalizeDouble(entryPrice - risk, g_SymbolDigits);
}

// Swing TP2: 测量移动(信号棒+前棒高度*200%) 或 强通道时用通道线; 保护: 不足1ATR则扩至1.5ATR
double GetMeasuredMoveTP2(const string &side, double entryPrice, double atr)
{
    if(atr <= 0) return 0;
    if(g_BufferSize < 3)
    {
        double fallback = atr * 2.0;
        return NormalizeDouble(side == "buy" ? entryPrice + fallback : entryPrice - fallback, g_SymbolDigits);
    }
    
    double tp2 = 0;
    bool useChannel = (g_MarketState == MARKET_STATE_TIGHT_CHANNEL && g_TightChannelExtreme > 0);
    
    if(useChannel)
    {
        if(side == "buy" && g_TightChannelDir == "up" && g_TightChannelExtreme > entryPrice)
            tp2 = g_TightChannelExtreme;
        else if(side == "sell" && g_TightChannelDir == "down" && g_TightChannelExtreme < entryPrice)
            tp2 = g_TightChannelExtreme;
    }
    
    if(tp2 <= 0)
    {
        double high12 = MathMax(g_HighBuffer[1], g_HighBuffer[2]);
        double low12  = MathMin(g_LowBuffer[1], g_LowBuffer[2]);
        double height = high12 - low12;
        if(height <= 0) height = atr * 0.5;
        double mappedDist = height * 2.0;
        if(side == "buy")  tp2 = entryPrice + mappedDist;
        else               tp2 = entryPrice - mappedDist;
    }
    
    double tp2Dist = (side == "buy") ? (tp2 - entryPrice) : (entryPrice - tp2);
    if(tp2Dist < atr * 1.0)
    {
        double minDist = atr * 1.5;
        if(side == "buy")  tp2 = entryPrice + minDist;
        else               tp2 = entryPrice - minDist;
    }
    return NormalizeDouble(tp2, g_SymbolDigits);
}

// 获取最近的N个swing high (使用缓存优化)
// allowTemp: 止损计算时可回退到临时波段点,降低结构更新延迟
double GetRecentSwingHigh(int nth, bool allowTemp = false)
{
    if(nth == 1 && g_CachedSH1 > 0) return g_CachedSH1;
    if(nth == 2 && g_CachedSH2 > 0) return g_CachedSH2;
    if(nth == 1 && allowTemp && g_TempSwingHigh > 0) return g_TempSwingHigh;
    
    int count = 0;
    for(int i = 0; i < g_SwingPointCount; i++)
    {
        if(g_SwingPoints[i].isHigh)
        {
            count++;
            if(count == nth) return g_SwingPoints[i].price;
        }
    }
    return 0;
}

// 获取最近的N个swing low (使用缓存优化)
double GetRecentSwingLow(int nth, bool allowTemp = false)
{
    if(nth == 1 && g_CachedSL1 > 0) return g_CachedSL1;
    if(nth == 2 && g_CachedSL2 > 0) return g_CachedSL2;
    if(nth == 1 && allowTemp && g_TempSwingLow > 0) return g_TempSwingLow;
    
    int count = 0;
    for(int i = 0; i < g_SwingPointCount; i++)
    {
        if(!g_SwingPoints[i].isHigh)
        {
            count++;
            if(count == nth) return g_SwingPoints[i].price;
        }
    }
    return 0;
}


//+------------------------------------------------------------------+
//| H/L Count - Brooks: 基于swing point的Push(推升)计数                  |
//| H1=第一次有效回调后突破前高, H2=第二次; L1/L2同理                   |
//| 重置: 不仅跌破前低/突破前高, 显著新极值(ATR倍数)或强反转信号也重置     |
//+------------------------------------------------------------------+
void UpdateHLCount(double atr)
{
    if(g_SwingPointCount < 4 || atr <= 0) return;
    
    double sh1 = 0, sl1 = 0, sh2 = 0, sl2 = 0;
    int shCount = 0, slCount = 0;
    
    for(int i = 0; i < g_SwingPointCount && (shCount < 2 || slCount < 2); i++)
    {
        if(g_SwingPoints[i].isHigh && shCount < 2)
        {
            if(shCount == 0) sh1 = g_SwingPoints[i].price;
            else sh2 = g_SwingPoints[i].price;
            shCount++;
        }
        else if(!g_SwingPoints[i].isHigh && slCount < 2)
        {
            if(slCount == 0) sl1 = g_SwingPoints[i].price;
            else sl2 = g_SwingPoints[i].price;
            slCount++;
        }
    }
    
    double resetExtremeATR = atr * InpHLResetNewExtremeATR;
    double minPullbackATR = atr * InpHLMinPullbackATR;
    
    // 强反转信号: 大实体反向K线,收盘在极值附近(Brooks climax-like)
    double currRange = g_HighBuffer[1] - g_LowBuffer[1];
    bool strongReversalDown = (currRange > atr * 0.8 && g_CloseBuffer[1] < g_OpenBuffer[1] &&
                              (g_HighBuffer[1] - g_CloseBuffer[1]) / MathMax(currRange, 1e-10) < 0.3);
    bool strongReversalUp   = (currRange > atr * 0.8 && g_CloseBuffer[1] > g_OpenBuffer[1] &&
                              (g_CloseBuffer[1] - g_LowBuffer[1]) / MathMax(currRange, 1e-10) < 0.3);
    
    // --- H计数(上升Push): 回调后突破前高 ---
    if(shCount >= 2 && slCount >= 1)
    {
        if(g_HighBuffer[1] > sh1 && sl1 < sh2 && (g_H_LastSwingHigh < sh1))
        {
            double pullbackDepth = sh2 - sl1;
            if(pullbackDepth >= minPullbackATR)
            {
                g_H_Count++;
                g_H_LastSwingHigh = sh1;
                g_H_LastPullbackLow = sl1;
                g_H_LastPBLowBar = 1;
            }
        }
        // 重置H计数: a)lower low b)显著新低(跌破前波段0.5ATR) c)强反转
        if(sl1 > 0 && sl2 > 0 && g_LowBuffer[1] < sl1 && sl1 < sl2)
            { g_H_Count = 0; g_H_LastSwingHigh = 0; g_H_LastPullbackLow = 0; }
        else if(sl1 > 0 && g_LowBuffer[1] < sl1 - resetExtremeATR)
            { g_H_Count = 0; g_H_LastSwingHigh = 0; g_H_LastPullbackLow = 0; }
        else if(strongReversalDown)
            { g_H_Count = 0; g_H_LastSwingHigh = 0; g_H_LastPullbackLow = 0; }
    }
    
    // --- L计数(下降Push): 反弹后跌破前低 ---
    if(slCount >= 2 && shCount >= 1)
    {
        if(g_LowBuffer[1] < sl1 && sh1 > sl2 && (g_L_LastSwingLow == 0 || sl1 < g_L_LastSwingLow))
        {
            double bounceDepth = sh1 - sl2;
            if(bounceDepth >= minPullbackATR)
            {
                g_L_Count++;
                g_L_LastSwingLow = sl1;
                g_L_LastBounceHigh = sh1;
                g_L_LastBounceBar = 1;
            }
        }
        // 重置L计数: a)higher high b)显著新高(超越前波段0.5ATR) c)强反转
        if(sh1 > 0 && sh2 > 0 && g_HighBuffer[1] > sh1 && sh1 > sh2)
            { g_L_Count = 0; g_L_LastSwingLow = 0; g_L_LastBounceHigh = 0; }
        else if(sh1 > 0 && g_HighBuffer[1] > sh1 + resetExtremeATR)
            { g_L_Count = 0; g_L_LastSwingLow = 0; g_L_LastBounceHigh = 0; }
        else if(strongReversalUp)
            { g_L_Count = 0; g_L_LastSwingLow = 0; g_L_LastBounceHigh = 0; }
    }
}

//+------------------------------------------------------------------+
//| H/L 计数信号 - 方向中性 (direction: DIR_LONG=1 多, DIR_SHORT=-1 空)  |
//| 统一 H1/H2 与 L1/L2 逻辑，减少重复                                 |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckHLCountSignal(int direction, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0) return SIGNAL_NONE;
    int count = (direction == DIR_LONG) ? g_H_Count : g_L_Count;
    ENUM_ALWAYS_IN needAI = (direction == DIR_LONG) ? AI_LONG : AI_SHORT;
    if(g_AlwaysIn != needAI) return SIGNAL_NONE;

    string sideStr = (direction == DIR_LONG) ? "buy" : "sell";
    double extreme = (direction == DIR_LONG) ? g_H_LastPullbackLow : g_L_LastBounceHigh;
    bool htfBlock = (direction == DIR_LONG && InpEnableHTFFilter && g_HTFTrendDir == "down") ||
                    (direction == DIR_SHORT && InpEnableHTFFilter && g_HTFTrendDir == "up");
    if(htfBlock) return SIGNAL_NONE;
    if(g_MarketState == MARKET_STATE_TRADING_RANGE) return SIGNAL_NONE;

    double close1 = g_CloseBuffer[1];
    stopLoss = (direction == DIR_LONG) ? (extreme - atr * 0.3) : (extreme + atr * 0.3);
    double risk = (direction == DIR_LONG) ? (close1 - stopLoss) : (stopLoss - close1);
    if(risk > atr * InpMaxStopATRMult) return SIGNAL_NONE;

    baseHeight = atr * 2.0;

    // 第一次计数(H1/L1): 需极强趋势 + 最近5根中至少4根同向
    if(count == 1)
    {
        bool isVeryStrong = (g_MarketState == MARKET_STATE_STRONG_TREND && g_TrendStrength >= 0.65) ||
                            (g_MarketState == MARKET_STATE_TIGHT_CHANNEL);
        int sameDirCount = 0;
        for(int i = 1; i <= 5 && i < g_BufferSize; i++)
        {
            double body = g_CloseBuffer[i] - g_OpenBuffer[i];
            if((direction == DIR_LONG && body > 0) || (direction == DIR_SHORT && body < 0))
                sameDirCount++;
        }
        if(!isVeryStrong || sameDirCount < 4) return SIGNAL_NONE;
        if(Check20GapBarBlock(direction == DIR_LONG ? "H1" : "L1")) return SIGNAL_NONE;
    }

    if(!CheckSignalCooldown(sideStr)) return SIGNAL_NONE;
    if(!ValidateSignalBar(sideStr)) return SIGNAL_NONE;

    if(direction == DIR_LONG) { g_H_Count = 0; UpdateSignalCooldown("buy"); return (count == 1) ? SIGNAL_H1_BUY : SIGNAL_H2_BUY; }
    else                      { g_L_Count = 0; UpdateSignalCooldown("sell"); return (count == 1) ? SIGNAL_L1_SELL : SIGNAL_L2_SELL; }
}

//+------------------------------------------------------------------+
//| Check Spike - Brooks: 连续3+根强趋势K线,几乎无重叠                 |
//| 不是2根大K线,而是一组连续的、方向一致的、重叠极少的K线               |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckSpike(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0) return SIGNAL_NONE;
    
    // 向上Spike检测: 连续N根阳线,低重叠,创新高
    int bullSpike = CountSpikeBarsBull(atr);
    if(bullSpike >= InpMinSpikeBars)
    {
        // Spike必须与AI方向一致,或足够强翻转AI
        if(g_AlwaysIn == AI_SHORT && bullSpike < 5) return SIGNAL_NONE;
        if(!ValidateSignalBar("buy") || !CheckSignalCooldown("buy")) return SIGNAL_NONE;
        
        // 入场: Spike后的第一根K线收盘确认
        if(g_CloseBuffer[1] > g_OpenBuffer[1]) // 确认K线
        {
            // 止损在Spike起点下方
            double spikeBottom = g_LowBuffer[1];
            for(int i = 1; i <= bullSpike + 1 && i < g_BufferSize; i++)
                if(g_LowBuffer[i] < spikeBottom) spikeBottom = g_LowBuffer[i];
            
            stopLoss = spikeBottom - atr * 0.3;
            if((g_CloseBuffer[1] - stopLoss) > atr * InpMaxStopATRMult)
            {
                // Spike太大,用最近swing low
                double recentSL = GetRecentSwingLow(1);
                if(recentSL > 0) stopLoss = recentSL - atr * 0.3;
                if((g_CloseBuffer[1] - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            }
            
            baseHeight = atr * 2.0;
            UpdateSignalCooldown("buy");
            return SIGNAL_SPIKE_BUY;
        }
    }
    
    // 向下Spike检测
    int bearSpike = CountSpikeBarsBear(atr);
    if(bearSpike >= InpMinSpikeBars)
    {
        if(g_AlwaysIn == AI_LONG && bearSpike < 5) return SIGNAL_NONE;
        if(!ValidateSignalBar("sell") || !CheckSignalCooldown("sell")) return SIGNAL_NONE;
        
        if(g_CloseBuffer[1] < g_OpenBuffer[1])
        {
            double spikeTop = g_HighBuffer[1];
            for(int i = 1; i <= bearSpike + 1 && i < g_BufferSize; i++)
                if(g_HighBuffer[i] > spikeTop) spikeTop = g_HighBuffer[i];
            
            stopLoss = spikeTop + atr * 0.3;
            if((stopLoss - g_CloseBuffer[1]) > atr * InpMaxStopATRMult)
            {
                double recentSH = GetRecentSwingHigh(1);
                if(recentSH > 0) stopLoss = recentSH + atr * 0.3;
                if((stopLoss - g_CloseBuffer[1]) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            }
            
            baseHeight = atr * 2.0;
            UpdateSignalCooldown("sell");
            return SIGNAL_SPIKE_SELL;
        }
    }
    
    return SIGNAL_NONE;
}

// 计算连续向上Spike K线数 - Brooks: 强趋势K线+低重叠
int CountSpikeBarsBull(double atr)
{
    int count = 0;
    int maxLookback = MathMin(20, g_BufferSize - 2);
    
    for(int i = 2; i <= maxLookback; i++)
    {
        double body = g_CloseBuffer[i] - g_OpenBuffer[i];
        double range = g_HighBuffer[i] - g_LowBuffer[i];
        if(range <= 0) break;
        
        // 趋势K线: 阳线且实体占比>50%
        bool isTrendBar = (body > 0 && body / range > 0.50);
        // 或者: 收盘在上半部分且range足够大
        if(!isTrendBar)
        {
            double closePos = (g_CloseBuffer[i] - g_LowBuffer[i]) / range;
            isTrendBar = (closePos > 0.6 && range > atr * 0.5);
        }
        if(!isTrendBar) break;
        
        // 重叠检查: 当前K线低点不应低于前一根K线中点太多
        if(i > 2 && i - 1 >= 1)
        {
            double prevMid = (g_HighBuffer[i-1] + g_LowBuffer[i-1]) / 2.0;
            double overlap = prevMid - g_LowBuffer[i];
            double prevRange = g_HighBuffer[i-1] - g_LowBuffer[i-1];
            if(prevRange > 0 && overlap / prevRange > InpSpikeOverlapMax) break;
        }
        
        count++;
    }
    return count;
}

// 计算连续向下Spike K线数
int CountSpikeBarsBear(double atr)
{
    int count = 0;
    int maxLookback = MathMin(20, g_BufferSize - 2);
    
    for(int i = 2; i <= maxLookback; i++)
    {
        double body = g_OpenBuffer[i] - g_CloseBuffer[i];
        double range = g_HighBuffer[i] - g_LowBuffer[i];
        if(range <= 0) break;
        
        bool isTrendBar = (body > 0 && body / range > 0.50);
        if(!isTrendBar)
        {
            double closePos = (g_HighBuffer[i] - g_CloseBuffer[i]) / range;
            isTrendBar = (closePos > 0.6 && range > atr * 0.5);
        }
        if(!isTrendBar) break;
        
        if(i > 2 && i - 1 >= 1)
        {
            double prevMid = (g_HighBuffer[i-1] + g_LowBuffer[i-1]) / 2.0;
            double overlap = g_HighBuffer[i] - prevMid;
            double prevRange = g_HighBuffer[i-1] - g_LowBuffer[i-1];
            if(prevRange > 0 && overlap / prevRange > InpSpikeOverlapMax) break;
        }
        
        count++;
    }
    return count;
}

//+------------------------------------------------------------------+
//| Check Micro Channel - Brooks: 极紧密通道,每根K线创新高/低           |
//| 回调极浅(不超过前一根25%),连续5+根方向一致                          |
//| 入场: Micro Channel中顺势突破前一根高/低点                          |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckMicroChannel(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || g_BufferSize < 8) return SIGNAL_NONE;
    
    // 检测向上Micro Channel: 连续K线创新高,回调极浅
    int upCount = 0;
    for(int i = 2; i <= 10 && i + 1 < g_BufferSize; i++)
    {
        // 每根K线高点高于前一根
        if(g_HighBuffer[i] <= g_HighBuffer[i+1]) break;
        // 低点也递增(higher lows)
        if(g_LowBuffer[i] < g_LowBuffer[i+1]) break;
        // 回调不超过前一根range的25%(Brooks标准)
        double prevRange = g_HighBuffer[i+1] - g_LowBuffer[i+1];
        if(prevRange > 0)
        {
            double prevThreshold = g_LowBuffer[i+1] + prevRange * 0.75;
            if(g_LowBuffer[i] < prevThreshold) break;
        }
        upCount++;
    }
    
    if(upCount >= 5 && g_AlwaysIn == AI_LONG)
    {
        // 入场: 当前K线突破前一根高点,且为阳线
        if(g_HighBuffer[1] > g_HighBuffer[2] && g_CloseBuffer[1] > g_OpenBuffer[1])
        {
            if(!ValidateSignalBar("buy") || !CheckSignalCooldown("buy")) return SIGNAL_NONE;
            
            // 止损在Micro Channel最低点下方
            double mcLow = g_LowBuffer[2];
            for(int i = 2; i <= upCount + 1 && i < g_BufferSize; i++)
                if(g_LowBuffer[i] < mcLow) mcLow = g_LowBuffer[i];
            
            stopLoss = mcLow - atr * 0.3;
            // MC止损可能很远,用最近2根低点作为替代
            if((g_CloseBuffer[1] - stopLoss) > atr * InpMaxStopATRMult)
                stopLoss = MathMin(g_LowBuffer[1], g_LowBuffer[2]) - atr * 0.3;
            if((g_CloseBuffer[1] - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            
            baseHeight = atr * 2.0;
            UpdateSignalCooldown("buy");
            return SIGNAL_MICRO_CH_BUY;
        }
    }
    
    // 检测向下Micro Channel
    int downCount = 0;
    for(int i = 2; i <= 10 && i + 1 < g_BufferSize; i++)
    {
        if(g_LowBuffer[i] >= g_LowBuffer[i+1]) break;
        if(g_HighBuffer[i] > g_HighBuffer[i+1]) break;
        double prevRange = g_HighBuffer[i+1] - g_LowBuffer[i+1];
        if(prevRange > 0)
        {
            // 回调不超过前一根range的25%
            double prevThreshold = g_HighBuffer[i+1] - prevRange * 0.75;
            if(g_HighBuffer[i] > prevThreshold) break;
        }
        downCount++;
    }
    
    if(downCount >= 5 && g_AlwaysIn == AI_SHORT)
    {
        if(g_LowBuffer[1] < g_LowBuffer[2] && g_CloseBuffer[1] < g_OpenBuffer[1])
        {
            if(!ValidateSignalBar("sell") || !CheckSignalCooldown("sell")) return SIGNAL_NONE;
            
            double mcHigh = g_HighBuffer[2];
            for(int i = 2; i <= downCount + 1 && i < g_BufferSize; i++)
                if(g_HighBuffer[i] > mcHigh) mcHigh = g_HighBuffer[i];
            
            stopLoss = mcHigh + atr * 0.3;
            if((stopLoss - g_CloseBuffer[1]) > atr * InpMaxStopATRMult)
                stopLoss = MathMax(g_HighBuffer[1], g_HighBuffer[2]) + atr * 0.3;
            if((stopLoss - g_CloseBuffer[1]) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            
            baseHeight = atr * 2.0;
            UpdateSignalCooldown("sell");
            return SIGNAL_MICRO_CH_SELL;
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Wedge - Brooks: 三推形态,推动力度递减,需突破趋势线            |
//| 三个递增高点(顶)或递减低点(底),每次推动动量减弱                     |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Wedge 三推形态 - 方向中性 (direction: DIR_LONG=1 楔底多, DIR_SHORT=-1 楔顶空) |
//| 统一 极值=extreme(i)、回撤、动量递减、当前K线 逻辑                     |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckWedgeDirection(int direction, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0) return SIGNAL_NONE;
    int lookback = 40;
    int maxIdx = MathMin(lookback, g_BufferSize - 3);
    // 极值: 多=low 找三底, 空=high 找三顶
    double ext[3] = {0, 0, 0};
    int extBars[3] = {-1, -1, -1};
    double extBodies[3] = {0, 0, 0};
    int extCount = 0;

    for(int i = 3; i <= maxIdx && extCount < 3; i++)
    {
        if(i - 2 < 0 || i + 2 >= g_BufferSize) continue;
        double ei = (direction == DIR_LONG) ? g_LowBuffer[i] : g_HighBuffer[i];
        double e1 = (direction == DIR_LONG) ? g_LowBuffer[i-1] : g_HighBuffer[i-1];
        double e2 = (direction == DIR_LONG) ? g_LowBuffer[i-2] : g_HighBuffer[i-2];
        double e3 = (direction == DIR_LONG) ? g_LowBuffer[i+1] : g_HighBuffer[i+1];
        double e4 = (direction == DIR_LONG) ? g_LowBuffer[i+2] : g_HighBuffer[i+2];
        bool isLocal = (direction == DIR_LONG) ? (ei < e1 && ei < e2 && ei < e3 && ei < e4) : (ei > e1 && ei > e2 && ei > e3 && ei > e4);
        if(!isLocal) continue;
        bool sequential = (extCount == 0) || ((direction == DIR_LONG && ei < ext[extCount-1]) || (direction == DIR_SHORT && ei > ext[extCount-1]));
        if(!sequential) continue;

        bool hasRetrace = true;
        if(extCount > 0)
        {
            int prevBar = extBars[extCount-1];
            double oppBetween = (direction == DIR_LONG) ? g_HighBuffer[i] : g_LowBuffer[i];
            for(int j = prevBar + 1; j < i && j < g_BufferSize; j++)
            {
                if(direction == DIR_LONG && g_HighBuffer[j] > oppBetween) oppBetween = g_HighBuffer[j];
                if(direction == DIR_SHORT && g_LowBuffer[j] < oppBetween) oppBetween = g_LowBuffer[j];
            }
            double retraceSize = (direction == DIR_LONG) ? (oppBetween - ext[extCount-1]) : (ext[extCount-1] - oppBetween);
            if(retraceSize < atr * 0.3) hasRetrace = false;
        }
        if(!hasRetrace) continue;

        double maxBody = 0;
        int startJ = (extCount > 0) ? extBars[extCount-1] : MathMin(i + 5, g_BufferSize - 1);
        for(int j = i; j <= startJ && j < g_BufferSize; j++)
        {
            double b = (direction == DIR_LONG) ? (g_OpenBuffer[j] - g_CloseBuffer[j]) : (g_CloseBuffer[j] - g_OpenBuffer[j]);
            if(b > maxBody) maxBody = b;
        }
        ext[extCount] = ei;
        extBars[extCount] = i;
        extBodies[extCount] = maxBody;
        extCount++;
    }

    if(extCount < 3) return SIGNAL_NONE;
    // Brooks: 楔形=三推动量递减，第三推最弱，故实体/力度应递减 extBodies[0]>extBodies[1]>extBodies[2]
    bool momentumDecline = (extBodies[0] > extBodies[1] && extBodies[1] > extBodies[2]);
    if(!momentumDecline) return SIGNAL_NONE;

    double currExt = (direction == DIR_LONG) ? g_LowBuffer[1] : g_HighBuffer[1];
    double currOpp = (direction == DIR_LONG) ? g_HighBuffer[1] : g_LowBuffer[1];
    bool nearThird = (MathAbs(currExt - ext[2]) <= atr * InpNearTrendlineATRMult);
    if(!nearThird) return SIGNAL_NONE;

    double kRange = g_HighBuffer[1] - g_LowBuffer[1];
    if(kRange <= 0) return SIGNAL_NONE;
    bool barDirection = (direction == DIR_LONG) ? (g_CloseBuffer[1] > g_OpenBuffer[1]) : (g_CloseBuffer[1] < g_OpenBuffer[1]);
    double closePos = (direction == DIR_LONG) ? ((g_CloseBuffer[1] - g_LowBuffer[1]) / kRange) : ((g_HighBuffer[1] - g_CloseBuffer[1]) / kRange);
    if(!barDirection || closePos < 0.50) return SIGNAL_NONE;

    string sideStr = (direction == DIR_LONG) ? "buy" : "sell";
    if(!CheckSignalCooldown(sideStr)) return SIGNAL_NONE;
    // 楔底多: 止损在第三底下方; 楔顶空: 止损在第三顶上方 (Brooks)
    stopLoss = ext[2] - (double)direction * atr * 0.5;
    baseHeight = (direction == DIR_LONG) ? (g_HighBuffer[extBars[0]] - ext[2]) : (ext[2] - g_LowBuffer[extBars[0]]);
    UpdateSignalCooldown(sideStr);
    return (direction == DIR_LONG) ? SIGNAL_WEDGE_BUY : SIGNAL_WEDGE_SELL;
}

//+------------------------------------------------------------------+
//| Check Climax Reversal - Brooks: 极端K线后的反转                    |
//| Climax = 超大range趋势K线(通常>2.5ATR),之后出现反向K线             |
//| 强趋势中第一次反转80%失败,需等第二入场                              |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckClimax(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0) return SIGNAL_NONE;
    
    // Climax K线 = bar[2], 反转确认 = bar[1]
    double prevHigh = g_HighBuffer[2], prevLow = g_LowBuffer[2];
    double prevOpen = g_OpenBuffer[2], prevClose = g_CloseBuffer[2];
    double prevRange = prevHigh - prevLow;
    double prevBody = MathAbs(prevClose - prevOpen);
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currOpen = g_OpenBuffer[1], currClose = g_CloseBuffer[1];
    double currRange = currHigh - currLow;
    double currBody = MathAbs(currClose - currOpen);
    
    if(currRange <= 0 || prevBody <= 0) return SIGNAL_NONE;
    
    bool isStrictMode = (g_MarketCycle == MARKET_CYCLE_SPIKE);
    double climaxMult = isStrictMode ? InpSpikeClimaxATRMult : 2.5;
    
    // --- 向上Climax -> 做空 ---
    if(prevRange > atr * climaxMult && prevClose > prevOpen)
    {
        // 反转K线: 阴线,收盘低于climax K线收盘
        if(currClose < currOpen && currClose < prevClose)
        {
            // 下影线不能太长(说明买方还在)
            double lowerTail = MathMin(currOpen, currClose) - currLow;
            if(currRange > 0 && lowerTail / currRange > 0.25) return SIGNAL_NONE;
            
            // 验证前期有足够的上涨空间
            double lookbackLow = g_LowBuffer[3];
            for(int i = 3; i <= 10 && i < g_BufferSize; i++)
                if(g_LowBuffer[i] < lookbackLow) lookbackLow = g_LowBuffer[i];
            
            double priorMove = prevHigh - lookbackLow;
            double minPrior = isStrictMode ? atr * 4.0 : atr * 2.0;
            if(priorMove < minPrior) return SIGNAL_NONE;
            
            // 第二入场检查 - Brooks: 强趋势中第一次反转通常失败
            if(isStrictMode && InpRequireSecondEntry)
            {
                if(!CheckForFailedReversalAttempt("bearish", atr))
                {
                    RecordReversalAttempt("bearish", currLow);
                    return SIGNAL_NONE;
                }
            }
            
            if(!CheckSignalCooldown("sell")) return SIGNAL_NONE;
            
            stopLoss = CalculateStopLoss("sell", atr);
            if(stopLoss > 0)
            {
                baseHeight = prevRange;
                UpdateSignalCooldown("sell");
                return SIGNAL_CLIMAX_SELL;
            }
        }
    }
    
    // --- 向下Climax -> 做多 ---
    if(prevRange > atr * climaxMult && prevClose < prevOpen)
    {
        if(currClose > currOpen && currClose > prevClose)
        {
            double upperTail = currHigh - MathMax(currOpen, currClose);
            if(currRange > 0 && upperTail / currRange > 0.25) return SIGNAL_NONE;
            
            double lookbackHigh = g_HighBuffer[3];
            for(int i = 3; i <= 10 && i < g_BufferSize; i++)
                if(g_HighBuffer[i] > lookbackHigh) lookbackHigh = g_HighBuffer[i];
            
            double priorMove = lookbackHigh - prevLow;
            double minPrior = isStrictMode ? atr * 4.0 : atr * 2.0;
            if(priorMove < minPrior) return SIGNAL_NONE;
            
            if(isStrictMode && InpRequireSecondEntry)
            {
                if(!CheckForFailedReversalAttempt("bullish", atr))
                {
                    RecordReversalAttempt("bullish", currHigh);
                    return SIGNAL_NONE;
                }
            }
            
            if(!CheckSignalCooldown("buy")) return SIGNAL_NONE;
            
            stopLoss = CalculateStopLoss("buy", atr);
            if(stopLoss > 0)
            {
                baseHeight = prevRange;
                UpdateSignalCooldown("buy");
                return SIGNAL_CLIMAX_BUY;
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check MTR - Brooks: Major Trend Reversal                          |
//| 完整流程: 趋势线突破 -> 回测趋势线失败 -> 形成higher low/lower high |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckMTR(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || !g_TrendLineBroken) return SIGNAL_NONE;
    
    // MTR需要: 1)趋势线已被突破 2)回测趋势线失败 3)形成反转结构
    
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currRange = currHigh - currLow;
    
    // 获取趋势线在当前位置的价格
    double tlPrice = GetTrendLinePrice(1);
    if(tlPrice <= 0) return SIGNAL_NONE;
    
    // --- 上升趋势线被突破 -> MTR Sell ---
    if(g_TrendDirection == "up" || g_AlwaysIn == AI_LONG)
    {
        // 趋势线在价格下方,被向下突破
        if(g_TrendLineBreakPrice > 0 && g_TrendLineBreakPrice < tlPrice)
        {
            // 回测: 价格反弹接近趋势线但未能突破,且出现反转K线形态(靠近=0.2*ATR)
            double nearTolerance = atr * InpNearTrendlineATRMult;
            bool retestFailed = false;
            for(int i = 1; i <= 5 && i < g_BufferSize; i++)
            {
                if(g_HighBuffer[i] >= tlPrice - nearTolerance && g_CloseBuffer[i] < tlPrice)
                {
                    // 检查是否有反转K线形态(阴线或doji)
                    double barRange = g_HighBuffer[i] - g_LowBuffer[i];
                    if(barRange > 0)
                    {
                        bool isBearish = g_CloseBuffer[i] < g_OpenBuffer[i];
                        bool isDoji = MathAbs(g_CloseBuffer[i] - g_OpenBuffer[i]) / barRange < 0.3;
                        if(isBearish || isDoji)
                        {
                            retestFailed = true;
                            break;
                        }
                    }
                }
            }
            
            if(retestFailed)
            {
                // 形成lower high: 最近swing high低于前一个
                double sh1 = GetRecentSwingHigh(1);
                double sh2 = GetRecentSwingHigh(2);
                if(sh1 > 0 && sh2 > 0 && sh1 < sh2)
                {
                    // 反转K线确认: 阴线且收盘在下半部
                    if(currClose < currOpen && currRange > 0)
                    {
                        double closePos = (currHigh - currClose) / currRange;
                        if(closePos >= 0.5 && ValidateSignalBar("sell") && CheckSignalCooldown("sell"))
                        {
                            stopLoss = sh1 + atr * 0.5;
                            baseHeight = sh2 - currLow;
                            UpdateSignalCooldown("sell");
                            g_TrendLineBroken = false;
                            return SIGNAL_MTR_SELL;
                        }
                    }
                }
            }
        }
    }
    
    // --- 下降趋势线被突破 -> MTR Buy ---
    if(g_TrendDirection == "down" || g_AlwaysIn == AI_SHORT)
    {
        if(g_TrendLineBreakPrice > 0 && g_TrendLineBreakPrice > tlPrice)
        {
            double nearTolerance = atr * InpNearTrendlineATRMult;
            bool retestFailed = false;
            for(int i = 1; i <= 5 && i < g_BufferSize; i++)
            {
                if(g_LowBuffer[i] <= tlPrice + nearTolerance && g_CloseBuffer[i] > tlPrice)
                {
                    double barRange = g_HighBuffer[i] - g_LowBuffer[i];
                    if(barRange > 0)
                    {
                        bool isBullish = g_CloseBuffer[i] > g_OpenBuffer[i];
                        bool isDoji = MathAbs(g_CloseBuffer[i] - g_OpenBuffer[i]) / barRange < 0.3;
                        if(isBullish || isDoji)
                        {
                            retestFailed = true;
                            break;
                        }
                    }
                }
            }
            
            if(retestFailed)
            {
                double sl1 = GetRecentSwingLow(1);
                double sl2 = GetRecentSwingLow(2);
                if(sl1 > 0 && sl2 > 0 && sl1 > sl2)
                {
                    if(currClose > currOpen && currRange > 0)
                    {
                        double closePos = (currClose - currLow) / currRange;
                        if(closePos >= 0.5 && ValidateSignalBar("buy") && CheckSignalCooldown("buy"))
                        {
                            stopLoss = sl1 - atr * 0.5;
                            baseHeight = currHigh - sl2;
                            UpdateSignalCooldown("buy");
                            g_TrendLineBroken = false;
                            return SIGNAL_MTR_BUY;
                        }
                    }
                }
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Trend Line Tracking - MTR需要趋势线突破和回测                      |
//+------------------------------------------------------------------+
void UpdateTrendLine(double atr)
{
    if(g_SwingPointCount < 4 || atr <= 0) return;
    
    // 上升趋势线: 连接两个递增的swing low
    if(g_AlwaysIn == AI_LONG || g_TrendDirection == "up")
    {
        double sl1 = 0, sl2 = 0;
        int sl1Bar = 0, sl2Bar = 0;
        int found = 0;
        
        for(int i = 0; i < g_SwingPointCount && found < 2; i++)
        {
            if(!g_SwingPoints[i].isHigh)
            {
                if(found == 0) { sl1 = g_SwingPoints[i].price; sl1Bar = g_SwingPoints[i].barIndex; }
                else { sl2 = g_SwingPoints[i].price; sl2Bar = g_SwingPoints[i].barIndex; }
                found++;
            }
        }
        
        if(found >= 2 && sl2 < sl1 && sl2Bar > sl1Bar)
        {
            g_TrendLineStart = sl2;
            g_TrendLineEnd = sl1;
            g_TrendLineStartBar = sl2Bar;
            g_TrendLineEndBar = sl1Bar;
            // 检查趋势线是否被突破
            double tlNow = GetTrendLinePrice(1);
            if(tlNow > 0 && g_CloseBuffer[1] < tlNow - atr * 0.1 && !g_TrendLineBroken)
            {
                g_TrendLineBroken = true;
                g_TrendLineBreakPrice = g_CloseBuffer[1];
            }
        }
    }
    
    // 下降趋势线: 连接两个递减的swing high
    if(g_AlwaysIn == AI_SHORT || g_TrendDirection == "down")
    {
        double sh1 = 0, sh2 = 0;
        int sh1Bar = 0, sh2Bar = 0;
        int found = 0;
        
        for(int i = 0; i < g_SwingPointCount && found < 2; i++)
        {
            if(g_SwingPoints[i].isHigh)
            {
                if(found == 0) { sh1 = g_SwingPoints[i].price; sh1Bar = g_SwingPoints[i].barIndex; }
                else { sh2 = g_SwingPoints[i].price; sh2Bar = g_SwingPoints[i].barIndex; }
                found++;
            }
        }
        
        if(found >= 2 && sh2 > sh1 && sh2Bar > sh1Bar)
        {
            g_TrendLineStart = sh2;
            g_TrendLineEnd = sh1;
            g_TrendLineStartBar = sh2Bar;
            g_TrendLineEndBar = sh1Bar;
            double tlNow = GetTrendLinePrice(1);
            if(tlNow > 0 && g_CloseBuffer[1] > tlNow + atr * 0.1 && !g_TrendLineBroken)
            {
                g_TrendLineBroken = true;
                g_TrendLineBreakPrice = g_CloseBuffer[1];
            }
        }
    }
}

double GetTrendLinePrice(int barIndex)
{
    // 防止除零错误
    if(g_TrendLineStartBar == g_TrendLineEndBar) 
    {
        // 如果两点重合,返回该点的价格
        return g_TrendLineEnd;
    }
    
    // 防止无效数据
    if(g_TrendLineStart == 0 || g_TrendLineEnd == 0)
        return 0;
    
    double slope = (g_TrendLineEnd - g_TrendLineStart) / 
                   (double)(g_TrendLineStartBar - g_TrendLineEndBar);
    return g_TrendLineEnd + slope * (g_TrendLineEndBar - barIndex);
}


//+------------------------------------------------------------------+
//| Check Failed Breakout - Brooks: TR边界突破失败后反向入场            |
//| 需要明确的TR上下边界,突破后快速回到TR内                             |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckFailedBreakout(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || g_TR_High <= 0 || g_TR_Low <= 0) return SIGNAL_NONE;
    
    double trRange = g_TR_High - g_TR_Low;
    if(trRange < atr * 1.0) return SIGNAL_NONE; // TR太窄
    
    double currHigh  = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double kRange = currHigh - currLow;
    if(kRange <= 0) return SIGNAL_NONE;
    
    // 向上突破失败: 突破TR高点后收回TR内
    if(currHigh > g_TR_High && currClose < g_TR_High && currClose < currOpen)
    {
        double closePos = (currHigh - currClose) / kRange;
        if(closePos >= 0.60 && CheckSignalCooldown("sell"))
        {
            stopLoss = currHigh + atr * 0.3;
            if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = trRange;
            UpdateSignalCooldown("sell");
            return SIGNAL_FAILED_BO_SELL;
        }
    }
    
    // 向下突破失败: 突破TR低点后收回TR内
    if(currLow < g_TR_Low && currClose > g_TR_Low && currClose > currOpen)
    {
        double closePos = (currClose - currLow) / kRange;
        if(closePos >= 0.60 && CheckSignalCooldown("buy"))
        {
            stopLoss = currLow - atr * 0.3;
            if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = trRange;
            UpdateSignalCooldown("buy");
            return SIGNAL_FAILED_BO_BUY;
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Final Flag - Brooks: Tight Channel结束后的最后一推            |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckFinalFlag(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(g_MarketState != MARKET_STATE_FINAL_FLAG) return SIGNAL_NONE;
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double kRange = currHigh - currLow;
    if(kRange <= 0 || atr <= 0) return SIGNAL_NONE;
    
    if(g_TightChannelDir == "up" && currClose < currOpen)
    {
        if((currHigh - currClose) / kRange >= 0.60 && ValidateSignalBar("sell") && CheckSignalCooldown("sell"))
        {
            stopLoss = g_TightChannelExtreme > 0 ? 
                       g_TightChannelExtreme + atr * 0.5 : currHigh + atr * 0.5;
            baseHeight = atr * 2.5;
            UpdateSignalCooldown("sell");
            return SIGNAL_FINAL_FLAG_SELL;
        }
    }
    else if(g_TightChannelDir == "down" && currClose > currOpen)
    {
        if((currClose - currLow) / kRange >= 0.60 && ValidateSignalBar("buy") && CheckSignalCooldown("buy"))
        {
            stopLoss = g_TightChannelExtreme > 0 ?
                       g_TightChannelExtreme - atr * 0.5 : currLow - atr * 0.5;
            baseHeight = atr * 2.5;
            UpdateSignalCooldown("buy");
            return SIGNAL_FINAL_FLAG_BUY;
        }
    }
    
    return SIGNAL_NONE;
}


//+------------------------------------------------------------------+
//| Check Double Top/Bottom - Brooks: 两次测试同一价位后反转            |
//| 方向中性: CheckDoubleTopBottomDirection(direction) 1=双底 -1=双顶   |
//| Double Top: 两次高点接近(容差0.3ATR),第二次未能突破+反转K线         |
//| Double Bottom: 两次低点接近,第二次未能跌破+反转K线                  |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckDoubleTopBottomDirection(int direction, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || g_SwingPointCount < 4) return SIGNAL_NONE;
    double level1 = (direction == DIR_LONG) ? GetRecentSwingLow(1) : GetRecentSwingHigh(1);
    double level2 = (direction == DIR_LONG) ? GetRecentSwingLow(2) : GetRecentSwingHigh(2);
    if(level1 <= 0 || level2 <= 0) return SIGNAL_NONE;

    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double kRange = currHigh - currLow;
    if(kRange <= 0) return SIGNAL_NONE;
    double tolerance = atr * 0.3;

    double currExt = (direction == DIR_LONG) ? currLow : currHigh;
    bool levelOk = (direction == DIR_LONG) ? (currLow <= level1 + tolerance) : (currHigh >= level1 - tolerance);
    bool barDir  = (direction == DIR_LONG) ? (currClose > currOpen) : (currClose < currOpen);
    double closePos = (direction == DIR_LONG) ? ((currClose - currLow) / kRange) : ((currHigh - currClose) / kRange);
    if(MathAbs(level1 - level2) > tolerance || !levelOk || !barDir || closePos < 0.55) return SIGNAL_NONE;

    string sideStr = (direction == DIR_LONG) ? "buy" : "sell";
    if(!CheckSignalCooldown(sideStr)) return SIGNAL_NONE;
    stopLoss = (direction == DIR_LONG) ? (MathMin(level1, level2) - atr * 0.3) : (MathMax(level1, level2) + atr * 0.3);
    double risk = (direction == DIR_LONG) ? (currClose - stopLoss) : (stopLoss - currClose);
    if(risk > atr * InpMaxStopATRMult) return SIGNAL_NONE;
    double sh1 = GetRecentSwingHigh(1), sh2 = GetRecentSwingHigh(2), sl1 = GetRecentSwingLow(1), sl2 = GetRecentSwingLow(2);
    double rangeHeight = MathMax(sh1, sh2) - MathMin(sl1, sl2);
    baseHeight = (rangeHeight > 0) ? rangeHeight : atr * 2.0;
    UpdateSignalCooldown(sideStr);
    return (direction == DIR_LONG) ? SIGNAL_DT_BUY : SIGNAL_DT_SELL;
}

//+------------------------------------------------------------------+
//| Check Trend Bar Entry - Brooks: 强趋势K线直接入场                  |
//| 条件: 大实体趋势K线(>0.7range) + 收盘在极端位置 + AI方向一致         |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckTrendBarEntry(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0) return SIGNAL_NONE;
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double kRange = currHigh - currLow;
    if(kRange <= 0 || kRange < atr * 0.8) return SIGNAL_NONE;
    
    double body = MathAbs(currClose - currOpen);
    double bodyRatio = body / kRange;
    if(bodyRatio < 0.70) return SIGNAL_NONE;
    
    if(currClose > currOpen && g_AlwaysIn == AI_LONG)
    {
        double closePos = (currClose - currLow) / kRange;
        if(closePos >= 0.75 && CheckSignalCooldown("buy"))
        {
            stopLoss = currLow - atr * 0.3;
            if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = kRange;
            UpdateSignalCooldown("buy");
            return SIGNAL_TREND_BAR_BUY;
        }
    }
    
    if(currClose < currOpen && g_AlwaysIn == AI_SHORT)
    {
        double closePos = (currHigh - currClose) / kRange;
        if(closePos >= 0.75 && CheckSignalCooldown("sell"))
        {
            stopLoss = currHigh + atr * 0.3;
            if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = kRange;
            UpdateSignalCooldown("sell");
            return SIGNAL_TREND_BAR_SELL;
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Reversal Bar Entry - Brooks: 反转K线入场                     |
//| 条件: 长影线+实体在反方向 + 前期有足够运动空间                       |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckReversalBarEntry(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0) return SIGNAL_NONE;
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double kRange = currHigh - currLow;
    if(kRange <= 0 || kRange < atr * 0.5) return SIGNAL_NONE;
    
    double body = MathAbs(currClose - currOpen);
    double upperTail = currHigh - MathMax(currClose, currOpen);
    double lowerTail = MathMin(currClose, currOpen) - currLow;
    
    double lookbackLow = currLow, lookbackHigh = currHigh;
    for(int i = 2; i <= 10 && i < g_BufferSize; i++)
    {
        if(g_LowBuffer[i] < lookbackLow) lookbackLow = g_LowBuffer[i];
        if(g_HighBuffer[i] > lookbackHigh) lookbackHigh = g_HighBuffer[i];
    }
    
    if(lowerTail > kRange * 0.4 && currClose > currOpen && lowerTail > body)
    {
        double priorDrop = currHigh - lookbackLow;
        if(priorDrop >= atr * 1.5 && CheckSignalCooldown("buy"))
        {
            stopLoss = currLow - atr * 0.3;
            if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = priorDrop;
            UpdateSignalCooldown("buy");
            return SIGNAL_REV_BAR_BUY;
        }
    }
    
    if(upperTail > kRange * 0.4 && currClose < currOpen && upperTail > body)
    {
        double priorRise = lookbackHigh - currLow;
        if(priorRise >= atr * 1.5 && CheckSignalCooldown("sell"))
        {
            stopLoss = currHigh + atr * 0.3;
            if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = priorRise;
            UpdateSignalCooldown("sell");
            return SIGNAL_REV_BAR_SELL;
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check ii/iii Pattern - Brooks: 连续内包线后的突破                   |
//| ii = 两根连续内包线, iii = 三根连续内包线                           |
//| 入场: 突破内包线序列的高/低点                                       |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckIIPattern(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || g_BufferSize < 7) return SIGNAL_NONE;
    
    int insideCount = 0;
    double patternHigh = g_HighBuffer[2];
    double patternLow = g_LowBuffer[2];
    
    // 确保不会越界: i最大为4, 需要访问i+1=5, 所以g_BufferSize需要>=7
    int maxCheck = MathMin(4, g_BufferSize - 3);
    for(int i = 2; i <= maxCheck; i++)
    {
        double motherHigh = g_HighBuffer[i + 1];
        double motherLow = g_LowBuffer[i + 1];
        double childHigh = g_HighBuffer[i];
        double childLow = g_LowBuffer[i];
        
        if(childHigh <= motherHigh && childLow >= motherLow)
        {
            insideCount++;
            if(childHigh > patternHigh) patternHigh = childHigh;
            if(childLow < patternLow) patternLow = childLow;
        }
        else
            break;
    }
    
    if(insideCount < 2) return SIGNAL_NONE;
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    
    if(currHigh > patternHigh && currClose > currOpen && CheckSignalCooldown("buy"))
    {
        stopLoss = patternLow - atr * 0.3;
        if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
        baseHeight = patternHigh - patternLow;
        UpdateSignalCooldown("buy");
        return SIGNAL_II_BUY;
    }
    
    if(currLow < patternLow && currClose < currOpen && CheckSignalCooldown("sell"))
    {
        stopLoss = patternHigh + atr * 0.3;
        if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
        baseHeight = patternHigh - patternLow;
        UpdateSignalCooldown("sell");
        return SIGNAL_II_SELL;
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Outside Bar Reversal - Brooks: 外包线反转                    |
//| 外包线完全包住前一根K线,收盘方向决定信号方向                         |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckOutsideBarReversal(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || g_BufferSize < 3) return SIGNAL_NONE;
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double prevHigh = g_HighBuffer[2], prevLow = g_LowBuffer[2];
    double kRange = currHigh - currLow;
    
    if(kRange <= 0) return SIGNAL_NONE;
    
    bool isOutsideBar = (currHigh > prevHigh && currLow < prevLow);
    if(!isOutsideBar) return SIGNAL_NONE;
    
    double body = MathAbs(currClose - currOpen);
    if(body / kRange < 0.40) return SIGNAL_NONE;
    
    double lookbackLow = currLow, lookbackHigh = currHigh;
    for(int i = 2; i <= 8 && i < g_BufferSize; i++)
    {
        if(g_LowBuffer[i] < lookbackLow) lookbackLow = g_LowBuffer[i];
        if(g_HighBuffer[i] > lookbackHigh) lookbackHigh = g_HighBuffer[i];
    }
    
    if(currClose > currOpen)
    {
        double priorDrop = currHigh - lookbackLow;
        if(priorDrop >= atr * 1.0 && CheckSignalCooldown("buy"))
        {
            stopLoss = currLow - atr * 0.3;
            if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = kRange;
            UpdateSignalCooldown("buy");
            return SIGNAL_OUTSIDE_BAR_BUY;
        }
    }
    
    if(currClose < currOpen)
    {
        double priorRise = lookbackHigh - currLow;
        if(priorRise >= atr * 1.0 && CheckSignalCooldown("sell"))
        {
            stopLoss = currHigh + atr * 0.3;
            if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = kRange;
            UpdateSignalCooldown("sell");
            return SIGNAL_OUTSIDE_BAR_SELL;
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Measured Move - Brooks: 等距运动目标                         |
//| AB=CD形态: 第二段运动等于第一段运动                                 |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckMeasuredMove(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || g_SwingPointCount < 4) return SIGNAL_NONE;
    
    double sh1 = GetRecentSwingHigh(1);
    double sh2 = GetRecentSwingHigh(2);
    double sl1 = GetRecentSwingLow(1);
    double sl2 = GetRecentSwingLow(2);
    
    if(sh1 <= 0 || sh2 <= 0 || sl1 <= 0 || sl2 <= 0) return SIGNAL_NONE;
    
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double tolerance = atr * 0.5;
    
    if(sl2 < sl1 && sh2 < sh1)
    {
        double leg1 = sh2 - sl2;
        double projectedTarget = sl1 + leg1;
        
        if(currHigh >= projectedTarget - tolerance && currHigh <= projectedTarget + tolerance)
        {
            if(currClose < currOpen && CheckSignalCooldown("sell"))
            {
                stopLoss = currHigh + atr * 0.3;
                if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
                baseHeight = leg1;
                UpdateSignalCooldown("sell");
                return SIGNAL_MEASURED_MOVE_SELL;
            }
        }
    }
    
    if(sh2 > sh1 && sl2 > sl1)
    {
        double leg1 = sh2 - sl2;
        double projectedTarget = sh1 - leg1;
        
        if(currLow <= projectedTarget + tolerance && currLow >= projectedTarget - tolerance)
        {
            if(currClose > currOpen && CheckSignalCooldown("buy"))
            {
                stopLoss = currLow - atr * 0.3;
                if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
                baseHeight = leg1;
                UpdateSignalCooldown("buy");
                return SIGNAL_MEASURED_MOVE_BUY;
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check TR Breakout - Brooks: Trading Range突破入场                  |
//| 条件: 强势突破K线 + 收盘在TR外 + 突破方向与AI一致                    |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckTRBreakout(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || g_TR_High <= 0 || g_TR_Low <= 0) return SIGNAL_NONE;
    if(IsTightTradingRange(atr)) return SIGNAL_NONE;  // TTR观望,不过早入场
    
    double trRange = g_TR_High - g_TR_Low;
    if(trRange < atr * 1.5) return SIGNAL_NONE;
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double kRange = currHigh - currLow;
    if(kRange <= 0) return SIGNAL_NONE;
    
    double body = MathAbs(currClose - currOpen);
    if(body / kRange < 0.50) return SIGNAL_NONE;
    
    if(currClose > g_TR_High && currClose > currOpen)
    {
        if(g_AlwaysIn != AI_SHORT && ValidateSignalBar("buy") && CheckSignalCooldown("buy"))
        {
            stopLoss = MathMax(currLow, g_TR_High - trRange * 0.3) - atr * 0.2;
            if((currClose - stopLoss) > atr * InpMaxStopATRMult)
                stopLoss = currLow - atr * 0.3;
            if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = trRange;
            UpdateSignalCooldown("buy");
            g_RecentBreakout = true;
            g_BreakoutDir = "up";
            g_BreakoutLevel = g_TR_High;
            g_BreakoutBarAge = 0;
            return SIGNAL_TR_BREAKOUT_BUY;
        }
    }
    
    if(currClose < g_TR_Low && currClose < currOpen)
    {
        if(g_AlwaysIn != AI_LONG && ValidateSignalBar("sell") && CheckSignalCooldown("sell"))
        {
            stopLoss = MathMin(currHigh, g_TR_Low + trRange * 0.3) + atr * 0.2;
            if((stopLoss - currClose) > atr * InpMaxStopATRMult)
                stopLoss = currHigh + atr * 0.3;
            if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = trRange;
            UpdateSignalCooldown("sell");
            g_RecentBreakout = true;
            g_BreakoutDir = "down";
            g_BreakoutLevel = g_TR_Low;
            g_BreakoutBarAge = 0;
            return SIGNAL_TR_BREAKOUT_SELL;
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Update Breakout Pullback Tracking                                 |
//+------------------------------------------------------------------+
void UpdateBreakoutPullbackTracking(double ema, double atr)
{
    if(!g_RecentBreakout) return;
    
    g_BreakoutBarAge++;
    
    // 根据市场状态动态调整过期时间
    int maxAge = 10;
    if(g_MarketState == MARKET_STATE_STRONG_TREND || g_MarketState == MARKET_STATE_BREAKOUT)
        maxAge = 15; // 强趋势中给更多时间等待回调
    else if(g_MarketState == MARKET_STATE_TRADING_RANGE)
        maxAge = 8;  // TR中回调应该更快
    
    if(g_BreakoutBarAge > maxAge)
    {
        g_RecentBreakout = false;
        g_BreakoutDir = "";
        g_BreakoutLevel = 0;
        g_BreakoutBarAge = 0;
    }
}

//+------------------------------------------------------------------+
//| Check Breakout Pullback - Brooks: 突破后回调入场                   |
//| 条件: 有效突破后 + 回调至突破位附近 + 反转K线确认                    |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckBreakoutPullback(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || !g_RecentBreakout || g_BreakoutLevel <= 0) return SIGNAL_NONE;
    if(g_BreakoutBarAge < 2 || g_BreakoutBarAge > 8) return SIGNAL_NONE;
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
    double tolerance = atr * 0.5;
    
    if(g_BreakoutDir == "up")
    {
        if(currLow <= g_BreakoutLevel + tolerance && currClose > currOpen)
        {
            if(currClose > g_BreakoutLevel && CheckSignalCooldown("buy"))
            {
                stopLoss = MathMin(currLow, g_BreakoutLevel) - atr * 0.3;
                if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
                baseHeight = atr * 2.0;
                UpdateSignalCooldown("buy");
                g_RecentBreakout = false;
                return SIGNAL_BO_PULLBACK_BUY;
            }
        }
    }
    
    if(g_BreakoutDir == "down")
    {
        if(currHigh >= g_BreakoutLevel - tolerance && currClose < currOpen)
        {
            if(currClose < g_BreakoutLevel && CheckSignalCooldown("sell"))
            {
                stopLoss = MathMax(currHigh, g_BreakoutLevel) + atr * 0.3;
                if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
                baseHeight = atr * 2.0;
                UpdateSignalCooldown("sell");
                g_RecentBreakout = false;
                return SIGNAL_BO_PULLBACK_SELL;
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Check Gap Bar - Brooks: 缺口K线入场                                |
//| 缺口K线 = 开盘价与前一根收盘价之间有明显跳空                         |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckGapBar(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(atr <= 0 || g_BufferSize < 3) return SIGNAL_NONE;
    
    double currOpen = g_OpenBuffer[1], currClose = g_CloseBuffer[1];
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double prevClose = g_CloseBuffer[2], prevHigh = g_HighBuffer[2], prevLow = g_LowBuffer[2];
    
    double gapThreshold = atr * 0.3;
    
    double gapUp = currOpen - prevHigh;
    double gapDown = prevLow - currOpen;
    
    if(gapUp >= gapThreshold && currClose > currOpen)
    {
        if(g_AlwaysIn == AI_LONG && ValidateSignalBar("buy") && CheckSignalCooldown("buy"))
        {
            stopLoss = MathMin(currLow, prevHigh) - atr * 0.3;
            if((currClose - stopLoss) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = atr * 2.0;
            UpdateSignalCooldown("buy");
            return SIGNAL_GAP_BAR_BUY;
        }
    }
    
    if(gapDown >= gapThreshold && currClose < currOpen)
    {
        if(g_AlwaysIn == AI_SHORT && ValidateSignalBar("sell") && CheckSignalCooldown("sell"))
        {
            stopLoss = MathMax(currHigh, prevLow) + atr * 0.3;
            if((stopLoss - currClose) > atr * InpMaxStopATRMult) return SIGNAL_NONE;
            baseHeight = atr * 2.0;
            UpdateSignalCooldown("sell");
            return SIGNAL_GAP_BAR_SELL;
        }
    }
    
    return SIGNAL_NONE;
}


//+------------------------------------------------------------------+
//| Market State Detection                                            |
//+------------------------------------------------------------------+
void DetectMarketState(double ema, double atr)
{
    ENUM_MARKET_STATE detectedState = MARKET_STATE_CHANNEL;
    
    if(DetectStrongTrend(ema, atr))
        detectedState = MARKET_STATE_STRONG_TREND;
    else if(DetectTightChannel(ema, atr))
    {
        detectedState = MARKET_STATE_TIGHT_CHANNEL;
        g_TightChannelBars++;
        UpdateTightChannelTracking();
    }
    else if(DetectFinalFlag(ema, atr))
    {
        detectedState = MARKET_STATE_FINAL_FLAG;
        if(g_TightChannelBars > 0) { g_LastTightChannelEndBar = 1; }
    }
    else if(DetectTradingRange(ema, atr))
    {
        detectedState = MARKET_STATE_TRADING_RANGE;
        if(g_TightChannelBars > 0) g_LastTightChannelEndBar = 1;
        g_TightChannelBars = 0;
    }
    else if(DetectBreakout(ema, atr))
        detectedState = MARKET_STATE_BREAKOUT;
    else
    {
        if(g_TightChannelBars > 0) g_LastTightChannelEndBar = 1;
        g_TightChannelBars = 0;
    }
    
    ApplyStateInertia(detectedState);
}

// Brooks: 强趋势 = 连续趋势K线 + higher highs/higher lows + 价格远离EMA
bool DetectStrongTrend(double ema, double atr)
{
    if(g_BufferSize < 12) return false;
    int lookback = 10;
    int bullishStreak = 0, bearishStreak = 0;
    int currentBullish = 0, currentBearish = 0;
    int higherHighs = 0, lowerLows = 0;
    int barsAboveEMA = 0, barsBelowEMA = 0;
    
    for(int i = 1; i <= lookback && i < g_BufferSize; i++)
    {
        bool isBullish = g_CloseBuffer[i] > g_OpenBuffer[i];
        bool isBearish = g_CloseBuffer[i] < g_OpenBuffer[i];
        
        if(isBullish) { currentBullish++; currentBearish = 0; }
        else if(isBearish) { currentBearish++; currentBullish = 0; }
        if(currentBullish > bullishStreak) bullishStreak = currentBullish;
        if(currentBearish > bearishStreak) bearishStreak = currentBearish;
        
        if(i + 1 < g_BufferSize)
        {
            if(g_HighBuffer[i] > g_HighBuffer[i+1]) higherHighs++;
            if(g_LowBuffer[i] < g_LowBuffer[i+1]) lowerLows++;
        }
        
        if(i < ArraySize(g_EMABuffer))
        {
            if(g_CloseBuffer[i] > g_EMABuffer[i]) barsAboveEMA++;
            else barsBelowEMA++;
        }
    }
    
    double upScore = 0, downScore = 0;
    
    if(bullishStreak >= 3) upScore += 0.25;
    if(bullishStreak >= 5) upScore += 0.25;
    if(higherHighs >= 4)   upScore += 0.2;
    if(barsAboveEMA >= 8)  upScore += 0.15;
    
    if(bearishStreak >= 3) downScore += 0.25;
    if(bearishStreak >= 5) downScore += 0.25;
    if(lowerLows >= 4)     downScore += 0.2;
    if(barsBelowEMA >= 8)  downScore += 0.15;
    
    // 价格距离EMA的程度
    if(atr > 0)
    {
        double dist = (g_CloseBuffer[1] - ema) / atr;
        if(dist > 1.0) upScore += 0.15;
        if(dist < -1.0) downScore += 0.15;
    }
    
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

// Micro Channel回调深度: Brooks原意是回调极浅(通常<25%)
bool DetectTightChannel(double ema, double atr)
{
    if(g_BufferSize < 15 || atr <= 0) return false;
    int lookback = 12;
    
    // 检查K线重叠程度和方向一致性
    int bullBars = 0, bearBars = 0;
    int consecutiveNewHighs = 0, consecutiveNewLows = 0;
    int shallowPullbacks = 0;
    
    for(int i = 1; i <= lookback && i + 1 < g_BufferSize; i++)
    {
        if(g_CloseBuffer[i] > g_OpenBuffer[i]) bullBars++;
        else if(g_CloseBuffer[i] < g_OpenBuffer[i]) bearBars++;
        
        // 创新高/新低
        if(g_HighBuffer[i] > g_HighBuffer[i+1]) consecutiveNewHighs++;
        if(g_LowBuffer[i] < g_LowBuffer[i+1]) consecutiveNewLows++;
        
        // 回调深度: 当前K线回调不超过前一根range的25%(Brooks标准更严格)
        double prevRange = g_HighBuffer[i+1] - g_LowBuffer[i+1];
        if(prevRange > 0)
        {
            // 上升TC: 当前低点不低于前一根75%位置
            double prevThreshold = g_LowBuffer[i+1] + prevRange * 0.75;
            if(g_LowBuffer[i] >= prevThreshold) shallowPullbacks++;
            // 下降TC: 当前高点不高于前一根25%位置
            double prevThresholdDown = g_HighBuffer[i+1] - prevRange * 0.75;
            if(g_HighBuffer[i] <= prevThresholdDown) shallowPullbacks++;
        }
    }
    
    // 上升Tight Channel
    if(bullBars >= lookback * 0.6 && consecutiveNewHighs >= lookback * 0.5 && shallowPullbacks >= lookback * 0.4)
    {
        g_TightChannelDir = "up";
        return true;
    }
    
    // 下降Tight Channel
    if(bearBars >= lookback * 0.6 && consecutiveNewLows >= lookback * 0.5 && shallowPullbacks >= lookback * 0.4)
    {
        g_TightChannelDir = "down";
        return true;
    }
    
    g_TightChannelDir = "";
    return false;
}

// Brooks: Trading Range = 价格在明确的高低边界内震荡
// 识别上下边界,而非仅靠EMA穿越次数
bool DetectTradingRange(double ema, double atr)
{
    if(g_BufferSize < 25 || atr <= 0) return false;
    int lookback = 20;
    
    // 找出lookback期间的高低点
    double rangeHigh = g_HighBuffer[1], rangeLow = g_LowBuffer[1];
    for(int i = 2; i <= lookback && i < g_BufferSize; i++)
    {
        if(g_HighBuffer[i] > rangeHigh) rangeHigh = g_HighBuffer[i];
        if(g_LowBuffer[i] < rangeLow)  rangeLow = g_LowBuffer[i];
    }
    
    double totalRange = rangeHigh - rangeLow;
    if(totalRange < atr * 2.0) return false; // 太窄不算TR
    
    // 检查价格是否在边界内来回震荡(多次触及上下边界)
    int touchHigh = 0, touchLow = 0;
    double upperZone = rangeHigh - totalRange * 0.2;
    double lowerZone = rangeLow + totalRange * 0.2;
    int emaCrosses = 0;
    bool prevAbove = g_CloseBuffer[lookback] > ema;
    
    for(int i = 1; i <= lookback && i < g_BufferSize; i++)
    {
        if(g_HighBuffer[i] >= upperZone) touchHigh++;
        if(g_LowBuffer[i] <= lowerZone) touchLow++;
        
        bool currAbove = g_CloseBuffer[i] > ema;
        if(currAbove != prevAbove) { emaCrosses++; prevAbove = currAbove; }
    }
    
    // TR条件: 多次触及上下边界 + EMA穿越频繁
    if(touchHigh >= 2 && touchLow >= 2 && emaCrosses >= 4)
    {
        g_TR_High = rangeHigh;
        g_TR_Low = rangeLow;
        return true;
    }
    
    return false;
}

// 20根棒线重叠度: 总范围/各棒range之和, 越小=重叠越高=越像紧凑区间
double GetBarOverlapRatio(int lookback = 20)
{
    if(g_BufferSize < lookback + 1) return 1.0;
    double rangeHigh = g_HighBuffer[1], rangeLow = g_LowBuffer[1];
    double sumRange = 0;
    for(int i = 1; i <= lookback && i < g_BufferSize; i++)
    {
        if(g_HighBuffer[i] > rangeHigh) rangeHigh = g_HighBuffer[i];
        if(g_LowBuffer[i] < rangeLow) rangeLow = g_LowBuffer[i];
        double barRange = g_HighBuffer[i] - g_LowBuffer[i];
        if(barRange > 0) sumRange += barRange;
    }
    double totalRange = rangeHigh - rangeLow;
    if(sumRange <= 0 || totalRange <= 0) return 1.0;
    return totalRange / sumRange;
}

// 紧凑交易区间(TTR): Brooks强调应观望,过滤突破与趋势信号
bool IsTightTradingRange(double atr)
{
    if(g_MarketState != MARKET_STATE_TRADING_RANGE || atr <= 0) return false;
    if(g_TR_High <= g_TR_Low) return false;
    double trRange = g_TR_High - g_TR_Low;
    if(trRange >= atr * InpTTRRangeATRMult) return false;  // 区间过宽不算TTR
    double overlapRatio = GetBarOverlapRatio(20);
    return (overlapRatio < InpTTROverlapThreshold);
}

bool DetectBreakout(double ema, double atr)
{
    if(g_BufferSize < 12 || atr <= 0) return false;
    
    double bodySize = MathAbs(g_CloseBuffer[1] - g_OpenBuffer[1]);
    double range = g_HighBuffer[1] - g_LowBuffer[1];
    if(range <= 0) return false;
    
    // Breakout K线: 实体大于平均实体1.5倍,收盘在极端位置
    double avgBody = 0;
    for(int i = 2; i <= 11 && i < g_BufferSize; i++)
        avgBody += MathAbs(g_CloseBuffer[i] - g_OpenBuffer[i]);
    avgBody /= 10;
    
    if(avgBody > 0 && bodySize > avgBody * 1.5)
    {
        double close = g_CloseBuffer[1];
        if(close > ema && (close - g_LowBuffer[1]) / range > 0.7) return true;
        if(close < ema && (g_HighBuffer[1] - close) / range > 0.7) return true;
    }
    return false;
}

bool DetectFinalFlag(double ema, double atr)
{
    if(g_TightChannelBars < 5 || g_LastTightChannelEndBar < 0) return false;
    
    int barsSince = g_LastTightChannelEndBar;
    if(barsSince < 3 || barsSince > 8) return false;
    
    if(atr <= 0) return false;
    double dist = (g_CloseBuffer[1] - ema) / atr;
    
    if(g_TightChannelDir == "up" && dist < 0.5) return false;
    if(g_TightChannelDir == "down" && dist > -0.5) return false;
    if(g_TightChannelDir == "") return false;
    
    return true;
}

void UpdateTightChannelTracking()
{
    if(g_TightChannelDir == "up")
    {
        if(g_TightChannelExtreme == 0 || g_HighBuffer[1] > g_TightChannelExtreme)
        { g_TightChannelExtreme = g_HighBuffer[1]; }
    }
    else if(g_TightChannelDir == "down")
    {
        if(g_TightChannelExtreme == 0 || g_LowBuffer[1] < g_TightChannelExtreme)
        { g_TightChannelExtreme = g_LowBuffer[1]; }
    }
}

void ApplyStateInertia(ENUM_MARKET_STATE newState)
{
    if(g_StateHoldBars > 0)
    {
        g_StateHoldBars--;
        g_MarketState = g_CurrentLockedState;
        return;
    }
    
    if(newState != g_CurrentLockedState)
    {
        int minHold = 1;
        switch(g_CurrentLockedState)
        {
            case MARKET_STATE_STRONG_TREND:  minHold = 3; break;
            case MARKET_STATE_TIGHT_CHANNEL: minHold = 3; break;
            case MARKET_STATE_TRADING_RANGE: minHold = 2; break;
            case MARKET_STATE_BREAKOUT:      minHold = 2; break;
            default: minHold = 1;
        }
        g_CurrentLockedState = newState;
        g_StateHoldBars = minHold;
    }
    
    if(g_MarketState != newState)
    {
        g_MarketState = newState;
    }
}

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
//| 实时波动率刷新 - Brooks Spike(波幅>1.5ATR)时更新,供止损防扫单          |
//| 节流: 至少间隔 5 秒执行，避免 Tick 内频繁 CopyBuffer                   |
//+------------------------------------------------------------------+
void RefreshRealTimeATR()
{
    if(TimeCurrent() - g_LastRefreshRealTimeATR < 5) return;
    g_LastRefreshRealTimeATR = TimeCurrent();
    
    int required = InpLookbackPeriod + 50;
    if(CopyBuffer(handleATR, 0, 0, required, g_ATRBuffer) < required) return;
    
    double baseAtr = (ArraySize(g_ATRBuffer) > 1) ? g_ATRBuffer[1] : 0;
    double currentRange = iHigh(_Symbol, PERIOD_CURRENT, 0) - iLow(_Symbol, PERIOD_CURRENT, 0);
    
    if(baseAtr > 0 && currentRange > baseAtr * 1.5)
        g_AtrValue = MathMax(baseAtr, currentRange / 1.5);
    else
        g_AtrValue = baseAtr;
}

//+------------------------------------------------------------------+
//| Market Data & HTF                                                 |
//+------------------------------------------------------------------+
bool GetMarketData()
{
    int required = InpLookbackPeriod + 50;
    
    if(CopyBuffer(handleEMA, 0, 0, required, g_EMABuffer) < required) return false;
    if(CopyBuffer(handleATR, 0, 0, required, g_ATRBuffer) < required) return false;
    if(CopyBuffer(handleHTFEMA, 0, 0, 10, g_HTFEMABuffer) < 5) return false;
    
    MqlRates rates[];
    ArraySetAsSeries(rates, true);
    int copied = CopyRates(_Symbol, PERIOD_CURRENT, 0, required, rates);
    if(copied < required) return false;
    
    ArrayResize(g_OpenBuffer, copied);
    ArrayResize(g_HighBuffer, copied);
    ArrayResize(g_LowBuffer, copied);
    ArrayResize(g_CloseBuffer, copied);
    ArrayResize(g_VolumeBuffer, copied);
    ArraySetAsSeries(g_OpenBuffer, true);
    ArraySetAsSeries(g_HighBuffer, true);
    ArraySetAsSeries(g_LowBuffer, true);
    ArraySetAsSeries(g_CloseBuffer, true);
    ArraySetAsSeries(g_VolumeBuffer, true);
    
    for(int i = 0; i < copied; i++)
    {
        g_OpenBuffer[i]   = rates[i].open;
        g_HighBuffer[i]   = rates[i].high;
        g_LowBuffer[i]    = rates[i].low;
        g_CloseBuffer[i]  = rates[i].close;
        g_VolumeBuffer[i] = rates[i].tick_volume;
    }
    
    g_BufferSize = copied;
    UpdateHTFTrend();
    return true;
}

void UpdateHTFTrend()
{
    if(ArraySize(g_HTFEMABuffer) < 3) return;
    
    double htfEMA = g_HTFEMABuffer[1];
    double currentClose = g_CloseBuffer[1];
    double atr = g_ATRBuffer[1];
    if(atr <= 0) return;
    
    double threshold = atr * 0.5;
    
    if(currentClose > htfEMA + threshold)
        g_HTFTrendDir = "up";
    else if(currentClose < htfEMA - threshold)
        g_HTFTrendDir = "down";
    else
        g_HTFTrendDir = "";
}

int CalculateGapCount(double ema, double atr)
{
    int count = 0;
    if(atr <= 0) return 0;
    double threshold = atr * 0.3;
    
    bool checkingUp   = g_CloseBuffer[1] > ema + threshold;
    bool checkingDown = g_CloseBuffer[1] < ema - threshold;
    
    if(!checkingUp && !checkingDown)
    {
        g_GapCount = 0;
        g_GapCountExtreme = 0;
        return 0;
    }
    
    int maxLookback = MathMin(50, g_BufferSize - 1);
    int maxEMA = ArraySize(g_EMABuffer) - 1;
    if(maxEMA < maxLookback) maxLookback = maxEMA;
    
    g_GapCountExtreme = checkingUp ? -DBL_MAX : DBL_MAX;
    
    for(int i = 1; i <= maxLookback; i++)
    {
        double barEMA = g_EMABuffer[i];
        
        if(checkingUp)
        {
            if(g_LowBuffer[i] > barEMA)
            {
                count++;
                if(g_HighBuffer[i] > g_GapCountExtreme)
                    g_GapCountExtreme = g_HighBuffer[i];
            }
            else break;
        }
        else
        {
            if(g_HighBuffer[i] < barEMA)
            {
                count++;
                if(g_LowBuffer[i] < g_GapCountExtreme)
                    g_GapCountExtreme = g_LowBuffer[i];
            }
            else break;
        }
    }
    
    g_GapCount = count;
    return count;
}


//+------------------------------------------------------------------+
//| 20 Gap Bar Rule                                                   |
//+------------------------------------------------------------------+
void Update20GapBarRule(double ema, double atr)
{
    if(!InpEnable20GapRule) return;
    if(atr <= 0) return;
    
    double threshold = atr * 0.3;
    bool priceAboveEMA   = g_CloseBuffer[1] > ema + threshold;
    bool priceBelowEMA   = g_CloseBuffer[1] < ema - threshold;
    bool priceTouchingEMA= !priceAboveEMA && !priceBelowEMA;
    
    if(!g_IsOverextended && g_GapCount >= InpGapBarThreshold)
    {
        g_IsOverextended = true;
        g_OverextendDirection = priceAboveEMA ? "up" : "down";
        g_OverextendStartTime = TimeCurrent();
        g_FirstPullbackBlocked = false;
        g_WaitingForRecovery = false;
        g_FirstPullbackComplete = false;
        g_ConsolidationCount = 0;
        g_PullbackExtreme = 0;
        if(InpDebugMode) Print("20 Gap Bar 触发: GapCount=", g_GapCount, " 方向=", g_OverextendDirection);
    }
    
    if(g_IsOverextended)
    {
        if(!g_FirstPullbackComplete && priceTouchingEMA)
        {
            if(!g_FirstPullbackBlocked)
            {
                g_FirstPullbackBlocked = true;
                g_WaitingForRecovery = true;
                g_PullbackExtreme = g_OverextendDirection == "up" ? g_LowBuffer[1] : g_HighBuffer[1];
            }
            g_ConsolidationCount++;
        }
        
        if(g_WaitingForRecovery)
        {
            bool recovered = false;
            
            // 横盘整理恢复
            if(g_ConsolidationCount >= InpConsolidationBars && atr > 0)
            {
                double rH = g_HighBuffer[1], rL = g_LowBuffer[1];
                for(int i = 2; i <= InpConsolidationBars && i < g_BufferSize; i++)
                {
                    if(g_HighBuffer[i] > rH) rH = g_HighBuffer[i];
                    if(g_LowBuffer[i] < rL) rL = g_LowBuffer[i];
                }
                if((rH - rL) <= atr * InpConsolidationRange) recovered = true;
            }
            
            // 双底/双顶恢复
            if(!recovered && g_PullbackExtreme > 0 && atr > 0)
            {
                double tol = atr * 0.3;
                if(g_OverextendDirection == "up" &&
                   g_LowBuffer[1] <= g_PullbackExtreme + tol && 
                   g_LowBuffer[1] >= g_PullbackExtreme - tol &&
                   g_CloseBuffer[1] > g_OpenBuffer[1])
                    recovered = true;
                if(g_OverextendDirection == "down" &&
                   g_HighBuffer[1] >= g_PullbackExtreme - tol && 
                   g_HighBuffer[1] <= g_PullbackExtreme + tol &&
                   g_CloseBuffer[1] < g_OpenBuffer[1])
                    recovered = true;
            }
            
            // 价格穿越EMA恢复
            if(!recovered)
            {
                if((g_OverextendDirection == "up" && priceBelowEMA) ||
                   (g_OverextendDirection == "down" && priceAboveEMA))
                    recovered = true;
            }
            
            if(recovered)
            {
                g_FirstPullbackComplete = true;
                g_WaitingForRecovery = false;
            }
        }
        
        // 重置条件: 需要连续2根K线确认方向改变,避免假突破
        bool shouldReset = false;
        if(g_GapCount == 0)
            shouldReset = true;
        else if(g_OverextendDirection == "up" && priceBelowEMA)
        {
            // 需要连续2根K线收盘在EMA下方才重置
            if(g_BufferSize >= 3 && g_CloseBuffer[2] < g_EMABuffer[2] - threshold)
                shouldReset = true;
        }
        else if(g_OverextendDirection == "down" && priceAboveEMA)
        {
            // 需要连续2根K线收盘在EMA上方才重置
            if(g_BufferSize >= 3 && g_CloseBuffer[2] > g_EMABuffer[2] + threshold)
                shouldReset = true;
        }
        
        if(shouldReset)
            Reset20GapBarState();
    }
}

void Reset20GapBarState()
{
    g_IsOverextended = false;
    g_FirstPullbackBlocked = false;
    g_OverextendDirection = "";
    g_OverextendStartTime = 0;
    g_WaitingForRecovery = false;
    g_ConsolidationCount = 0;
    g_PullbackExtreme = 0;
    g_FirstPullbackComplete = false;
}

bool Check20GapBarBlock(string signalType)
{
    if(!InpEnable20GapRule || !InpBlockFirstPullback) return false;
    if(!g_IsOverextended || !g_FirstPullbackBlocked || !g_WaitingForRecovery) return false;
    
    if((signalType == "H1" && g_OverextendDirection == "up") ||
       (signalType == "L1" && g_OverextendDirection == "down"))
        return true;
    
    return false;
}

//+------------------------------------------------------------------+
//| Reversal Attempt Tracking                                         |
//+------------------------------------------------------------------+
void RecordReversalAttempt(string direction, double extremePrice)
{
    g_LastReversalAttempt.time = TimeCurrent();
    g_LastReversalAttempt.price = extremePrice;
    g_LastReversalAttempt.direction = direction;
    g_LastReversalAttempt.failed = false;
    g_HasPendingReversal = true;
    g_ReversalAttemptCount++;
}

bool CheckForFailedReversalAttempt(string direction, double atr)
{
    if(!g_HasPendingReversal) return false;
    if(g_LastReversalAttempt.direction != direction)
    {
        ClearReversalAttempt();
        return false;
    }
    
    int maxTimeDiff = PeriodSeconds(PERIOD_CURRENT) * InpSecondEntryLookback;
    datetime currentBarTime = iTime(_Symbol, PERIOD_CURRENT, 1);
    
    if(currentBarTime - g_LastReversalAttempt.time > maxTimeDiff)
    {
        ClearReversalAttempt();
        return false;
    }
    
    double extremePrice = g_LastReversalAttempt.price;
    int maxLookback = MathMin(InpSecondEntryLookback, g_BufferSize);
    bool failed = false;
    
    if(direction == "bearish")
    {
        for(int i = 1; i < maxLookback; i++)
            if(g_HighBuffer[i] > extremePrice + atr * 0.1) { failed = true; break; }
    }
    else
    {
        for(int i = 1; i < maxLookback; i++)
            if(g_LowBuffer[i] < extremePrice - atr * 0.1) { failed = true; break; }
    }
    
    if(failed && !g_LastReversalAttempt.failed)
        g_LastReversalAttempt.failed = true;
    
    return g_LastReversalAttempt.failed;
}

void ClearReversalAttempt()
{
    g_HasPendingReversal = false;
    g_ReversalAttemptCount = 0;
    g_LastReversalAttempt.time = 0;
    g_LastReversalAttempt.price = 0;
    g_LastReversalAttempt.direction = "";
    g_LastReversalAttempt.failed = false;
}

void UpdateReversalAttemptTracking()
{
    bool isStrongTrend = (g_MarketState == MARKET_STATE_STRONG_TREND ||
                          g_MarketState == MARKET_STATE_BREAKOUT ||
                          g_MarketCycle == MARKET_CYCLE_SPIKE);
    
    if(!isStrongTrend && g_HasPendingReversal)
    {
        ClearReversalAttempt();
        return;
    }
    
    if(g_HasPendingReversal && !g_LastReversalAttempt.failed)
    {
        double atr = g_ATRBuffer[1];
        if(atr > 0) CheckForFailedReversalAttempt(g_LastReversalAttempt.direction, atr);
    }
    
    if(g_HasPendingReversal && g_LastReversalAttempt.failed)
    {
        int maxTimeDiff = PeriodSeconds(PERIOD_CURRENT) * InpSecondEntryLookback * 2;
        if(TimeCurrent() - g_LastReversalAttempt.time > maxTimeDiff)
            ClearReversalAttempt();
    }
    
    if(g_HasPendingReversal)
    {
        if((g_LastReversalAttempt.direction == "bullish" && g_TrendDirection == "down") ||
           (g_LastReversalAttempt.direction == "bearish" && g_TrendDirection == "up"))
            ClearReversalAttempt();
    }
}


//+------------------------------------------------------------------+
//| Validate Signal Bar - Brooks: 信号K线必须方向正确,实体足够大        |
//+------------------------------------------------------------------+
bool ValidateSignalBar(string side)
{
    double high = g_HighBuffer[1], low = g_LowBuffer[1];
    double open = g_OpenBuffer[1], close = g_CloseBuffer[1];
    double kRange = high - low;
    if(kRange <= 0) return false;
    
    double body = MathAbs(close - open);
    double bodyRatio = body / kRange;
    if(bodyRatio < InpMinBodyRatio) return false;
    
    if(side == "buy" && close <= open) return false;
    if(side == "sell" && close >= open) return false;
    
    // 收盘在极端位置
    double upperTail = high - MathMax(close, open);
    double lowerTail = MathMin(close, open) - low;
    if(side == "buy" && upperTail / kRange > InpClosePositionPct) return false;
    if(side == "sell" && lowerTail / kRange > InpClosePositionPct) return false;
    
    return true;
}

//+------------------------------------------------------------------+
//| Stop Loss Calculation - Brooks: 止损在结构位(swing point)          |
//+------------------------------------------------------------------+
double CalculateStopLoss(string side, double atr)
{
    double entryPrice = side == "buy" ? 
                        SymbolInfoDouble(_Symbol, SYMBOL_ASK) : 
                        SymbolInfoDouble(_Symbol, SYMBOL_BID);
    return CalculateUnifiedStopLoss(side, atr, entryPrice);
}

double CalculateUnifiedStopLoss(string side, double atr, double entryPrice)
{
    double spreadPrice = GetCurrentSpreadPrice();
    
    bool isStrongTrend = (g_MarketState == MARKET_STATE_STRONG_TREND ||
                          g_MarketState == MARKET_STATE_BREAKOUT ||
                          g_MarketState == MARKET_STATE_TIGHT_CHANNEL);
    
    double atrBuffer = atr > 0 ? (isStrongTrend ? atr * 0.3 : atr * 0.5) : 0;
    double minBuf = (atr > 0) ? atr * InpMinBufferATRMult : 0;
    double totalBuffer = MathMax(atrBuffer, minBuf) + spreadPrice;
    
    double stopLoss = 0;
    double stopDistance = 0;
    
    if(isStrongTrend)
    {
        // 强趋势: 信号K线止损
        if(side == "buy")
        {
            stopLoss = MathMin(g_LowBuffer[1], g_LowBuffer[2]) - totalBuffer;
            stopDistance = entryPrice - stopLoss;
        }
        else
        {
            stopLoss = MathMax(g_HighBuffer[1], g_HighBuffer[2]) + totalBuffer;
            stopDistance = stopLoss - entryPrice;
        }
    }
    else
    {
        // 非强趋势: 用swing point止损
        if(side == "buy")
        {
            double swingLow = GetRecentSwingLow(1, true);  // allowTemp 降低结构延迟
            if(swingLow > 0 && (entryPrice - swingLow - totalBuffer) <= atr * InpMaxStopATRMult)
                stopLoss = swingLow - totalBuffer;
            else
                stopLoss = MathMin(g_LowBuffer[1], g_LowBuffer[2]) - totalBuffer;
            stopDistance = entryPrice - stopLoss;
        }
        else
        {
            double swingHigh = GetRecentSwingHigh(1, true);  // allowTemp 降低结构延迟
            if(swingHigh > 0 && (swingHigh + totalBuffer - entryPrice) <= atr * InpMaxStopATRMult)
                stopLoss = swingHigh + totalBuffer;
            else
                stopLoss = MathMax(g_HighBuffer[1], g_HighBuffer[2]) + totalBuffer;
            stopDistance = stopLoss - entryPrice;
        }
    }
    
    if(atr > 0 && stopDistance > atr * InpMaxStopATRMult)
        return 0;
    
    return NormalizeDouble(stopLoss, g_SymbolDigits);
}

double GetCurrentSpreadPrice()
{
    return SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID);
}

//+------------------------------------------------------------------+
//| Al Brooks 统一止损: 最近 swing 外 + 缓冲，超过 3ATR 用 K 线极值并封顶
//+------------------------------------------------------------------+
double GetBrooksStopLoss(const string &side, double entryPrice, double atr)
{
    double spread = GetCurrentSpreadPrice();
    double buf    = (atr > 0 ? atr * 0.3 : 0) + spread;
    double minBuf = (atr > 0) ? atr * InpMinBufferATRMult : 0;
    if(buf < minBuf) buf = minBuf;
    
    if(side == "buy")
    {
        double swingLow = GetRecentSwingLow(1, true);  // allowTemp 降低结构延迟
        if(swingLow > 0 && swingLow < entryPrice)
        {
            double dist = entryPrice - swingLow;
            if(atr <= 0 || dist <= atr * InpMaxStopATRMult)
                return NormalizeDouble(swingLow - buf, g_SymbolDigits);
        }
        double barLow = (g_BufferSize >= 2) ? MathMin(g_LowBuffer[1], g_LowBuffer[2]) : (g_BufferSize >= 1 ? g_LowBuffer[1] : 0);
        if(barLow <= 0) return 0;
        double sl = barLow - buf;
        if(sl >= entryPrice) sl = entryPrice - (atr > 0 ? atr * 0.3 : buf);
        if(atr > 0 && (entryPrice - sl) > atr * InpMaxStopATRMult)
            sl = entryPrice - atr * InpMaxStopATRMult;
        return NormalizeDouble(sl, g_SymbolDigits);
    }
    else
    {
        double swingHigh = GetRecentSwingHigh(1, true);  // allowTemp 降低结构延迟
        if(swingHigh > 0 && swingHigh > entryPrice)
        {
            double dist = swingHigh - entryPrice;
            if(atr <= 0 || dist <= atr * InpMaxStopATRMult)
                return NormalizeDouble(swingHigh + buf, g_SymbolDigits);
        }
        double barHigh = (g_BufferSize >= 2) ? MathMax(g_HighBuffer[1], g_HighBuffer[2]) : (g_BufferSize >= 1 ? g_HighBuffer[1] : 0);
        if(barHigh <= 0) return 0;
        double sl = barHigh + buf;
        if(sl <= entryPrice) sl = entryPrice + (atr > 0 ? atr * 0.3 : buf);
        if(atr > 0 && (sl - entryPrice) > atr * InpMaxStopATRMult)
            sl = entryPrice + atr * InpMaxStopATRMult;
        return NormalizeDouble(sl, g_SymbolDigits);
    }
}

//+------------------------------------------------------------------+
//| Signal Cooldown                                                   |
//+------------------------------------------------------------------+
bool CheckSignalCooldown(string side)
{
    int currentBar = Bars(_Symbol, PERIOD_CURRENT);
    double currentPrice = side == "buy" ? 
                          SymbolInfoDouble(_Symbol, SYMBOL_ASK) : 
                          SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double atr = g_ATRBuffer[1];
    
    if(side == "buy")
    {
        // K线冷却期检查
        if(currentBar - g_LastBuySignalBar < InpSignalCooldown) return false;
        
        // 价格距离检查: 考虑市场已经大幅移动后回到原位的情况
        if(g_LastBuyEntryPrice > 0 && atr > 0)
        {
            double priceDiff = MathAbs(currentPrice - g_LastBuyEntryPrice);
            // 如果价格距离太近,检查是否有足够的波动(说明市场已经移动过)
            if(priceDiff < atr * 1.5)
            {
                // 检查最近N根K线的波动范围
                double rangeHigh = g_HighBuffer[1], rangeLow = g_LowBuffer[1];
                int checkBars = MathMin(InpSignalCooldown + 2, g_BufferSize - 1);
                for(int i = 2; i <= checkBars; i++)
                {
                    if(g_HighBuffer[i] > rangeHigh) rangeHigh = g_HighBuffer[i];
                    if(g_LowBuffer[i] < rangeLow) rangeLow = g_LowBuffer[i];
                }
                double totalRange = rangeHigh - rangeLow;
                // 如果波动范围小于2ATR,说明市场没有足够移动,拒绝信号
                if(totalRange < atr * 2.0)
                    return false;
            }
        }
    }
    else
    {
        if(currentBar - g_LastSellSignalBar < InpSignalCooldown) return false;
        
        if(g_LastSellEntryPrice > 0 && atr > 0)
        {
            double priceDiff = MathAbs(g_LastSellEntryPrice - currentPrice);
            if(priceDiff < atr * 1.5)
            {
                double rangeHigh = g_HighBuffer[1], rangeLow = g_LowBuffer[1];
                int checkBars = MathMin(InpSignalCooldown + 2, g_BufferSize - 1);
                for(int i = 2; i <= checkBars; i++)
                {
                    if(g_HighBuffer[i] > rangeHigh) rangeHigh = g_HighBuffer[i];
                    if(g_LowBuffer[i] < rangeLow) rangeLow = g_LowBuffer[i];
                }
                double totalRange = rangeHigh - rangeLow;
                if(totalRange < atr * 2.0)
                    return false;
            }
        }
    }
    return true;
}

void UpdateSignalCooldown(string side)
{
    int currentBar = Bars(_Symbol, PERIOD_CURRENT);
    if(side == "buy")
    {
        g_LastBuySignalBar = currentBar;
        g_LastBuySignalTime = TimeCurrent();
        g_LastBuyEntryPrice = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    }
    else
    {
        g_LastSellSignalBar = currentBar;
        g_LastSellSignalTime = TimeCurrent();
        g_LastSellEntryPrice = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    }
}


//+------------------------------------------------------------------+
//| Order Execution - Brooks: 所有入场用Stop Order确认方向              |
//| Spike顺势可市价,其余全部Stop Order                                 |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE DetermineOrderType(ENUM_SIGNAL_TYPE signal, string side)
{
    // Spike信号: 市价入场(已有连续确认K线)
    if(signal == SIGNAL_SPIKE_BUY || signal == SIGNAL_SPIKE_SELL)
        return side == "buy" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
    
    // 其余所有信号: Stop Order确认方向
    if(InpUseStopOrders)
        return side == "buy" ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP;
    
    return side == "buy" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
}

// 挂单价 = 信号K线(bar[1])高点/低点 ± 偏移；不根据当前价改写成“近似市价”，由交易所/券商在触价时执行
double CalculateStopOrderPrice(string side)
{
    double price = 0;
    double tickSize = (g_SymbolTickSize > 0) ? g_SymbolTickSize : g_SymbolPoint;
    if(side == "buy")
        price = g_HighBuffer[1] + (InpStopOrderOffset > 0 ? InpStopOrderOffset * tickSize : tickSize);
    else
        price = g_LowBuffer[1] - (InpStopOrderOffset > 0 ? InpStopOrderOffset * tickSize : tickSize);
    return NormalizeDouble(price, g_SymbolDigits);
}

//+------------------------------------------------------------------+
//| 两笔单模式: 手数规范化、配对注释、平掉配对仓位                        |
//+------------------------------------------------------------------+
double NormalizeLot(double lot)
{
    double volumeMin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double volumeStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    if(lot < volumeMin) return volumeMin;
    if(volumeStep > 0)
        lot = MathFloor(lot / volumeStep) * volumeStep;
    if(lot < volumeMin) lot = volumeMin;
    return NormalizeDouble(lot, 2);
}

// (原 GetCommentBase/FindPairTicket/ClosePairIfExists 已移除：配对改由 Twin Magic 区分 Scalp/Runner)

//+------------------------------------------------------------------+
//| 方案A: 挂单校验，不满足则跳过(符合 Brooks 不追单)                    |
//| 校验: 挂单价与市价距离、挂单价与 SL/TP 距离 >= SYMBOL_TRADE_STOPS_LEVEL |
//+------------------------------------------------------------------+
bool ValidateStopOrder(string side, double stopOrderPrice, double brokerSL, double tp, string &reason)
{
    long stopLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
    int effectivePoints = (int)MathMax((long)InpMinStopsLevelPoints, stopLevel);
    double minDist = effectivePoints * g_SymbolPoint;
    double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    
    if(side == "buy")
    {
        if(stopOrderPrice <= ask + minDist)
        {
            reason = "BuyStop距市价过近或已越过 Ask=" + DoubleToString(ask, g_SymbolDigits);
            return false;
        }
        if(MathAbs(stopOrderPrice - brokerSL) < minDist)
        {
            reason = "BuyStop与SL距离不足 minDist=" + DoubleToString(minDist, g_SymbolDigits);
            return false;
        }
        if(tp > 0 && MathAbs(stopOrderPrice - tp) < minDist)
        {
            reason = "BuyStop与TP距离不足 minDist=" + DoubleToString(minDist, g_SymbolDigits);
            return false;
        }
    }
    else
    {
        if(stopOrderPrice >= bid - minDist)
        {
            reason = "SellStop距市价过近或已越过 Bid=" + DoubleToString(bid, g_SymbolDigits);
            return false;
        }
        if(MathAbs(stopOrderPrice - brokerSL) < minDist)
        {
            reason = "SellStop与SL距离不足 minDist=" + DoubleToString(minDist, g_SymbolDigits);
            return false;
        }
        if(tp > 0 && MathAbs(stopOrderPrice - tp) < minDist)
        {
            reason = "SellStop与TP距离不足 minDist=" + DoubleToString(minDist, g_SymbolDigits);
            return false;
        }
    }
    return true;
}

//+------------------------------------------------------------------+
//| 带重试的交易执行 - RETCODE_REQUOTE/PRICE_CHANGED/LOCKED/ContextBusy 时重试 |
//| 返回 true=成功; 失败时由调用方记录详细日志(挂单价/SL/买卖价)              |
//+------------------------------------------------------------------+
bool ExecuteTradeWithRetry(int magic, string side, bool isStopOrder, double lot,
                          double stopOrderPrice, double brokerSL, double tp,
                          datetime expiration, string comment)
{
    trade.SetExpertMagicNumber(magic);
    trade.SetDeviationInPoints((ulong)MathMax(1, InpMaxSlippage));
    
    bool ok = false;
    for(int retry = 0; retry < 3; retry++)
    {
        if(isStopOrder)
        {
            if(side == "buy")
                ok = trade.BuyStop(lot, stopOrderPrice, _Symbol, brokerSL, tp, ORDER_TIME_SPECIFIED, expiration, comment);
            else
                ok = trade.SellStop(lot, stopOrderPrice, _Symbol, brokerSL, tp, ORDER_TIME_SPECIFIED, expiration, comment);
        }
        else
        {
            if(side == "buy")
                ok = trade.Buy(lot, _Symbol, 0, brokerSL, tp, comment);
            else
                ok = trade.Sell(lot, _Symbol, 0, brokerSL, tp, comment);
        }
        if(ok) return true;
        
        uint retcode = trade.ResultRetcode();
        int err = GetLastError();
        bool retryable = (retcode == TRADE_RETCODE_REQUOTE || retcode == TRADE_RETCODE_PRICE_CHANGED ||
                          retcode == TRADE_RETCODE_LOCKED || err == 146);
        if(!retryable || retry >= 3) break;  // 至少3次重试(共4次尝试)
        Sleep(100);
    }
    return false;
}

// 平仓带重试 - REQUOTE/PRICE_CHANGED 时至少重试3次
bool PositionCloseWithRetry(ulong ticket)
{
    for(int retry = 0; retry < 4; retry++)
    {
        if(trade.PositionClose(ticket)) return true;
        uint retcode = trade.ResultRetcode();
        if(retcode != TRADE_RETCODE_REQUOTE && retcode != TRADE_RETCODE_PRICE_CHANGED) break;
        if(retry < 3) Sleep(100);
    }
    return false;
}

// 分批平仓带重试 - REQUOTE/PRICE_CHANGED 时至少重试3次
bool PositionClosePartialWithRetry(ulong ticket, double volume)
{
    for(int retry = 0; retry < 4; retry++)
    {
        if(trade.PositionClosePartial(ticket, volume)) return true;
        uint retcode = trade.ResultRetcode();
        if(retcode != TRADE_RETCODE_REQUOTE && retcode != TRADE_RETCODE_PRICE_CHANGED) break;
        if(retry < 3) Sleep(100);
    }
    return false;
}

// 修改止损/止盈带重试 - REQUOTE/PRICE_CHANGED/LOCKED 时重试
bool PositionModifyWithRetry(ulong ticket, double sl, double tp)
{
    for(int retry = 0; retry < 4; retry++)
    {
        if(trade.PositionModify(ticket, sl, tp)) return true;
        uint retcode = trade.ResultRetcode();
        bool retryable = (retcode == TRADE_RETCODE_REQUOTE || retcode == TRADE_RETCODE_PRICE_CHANGED || retcode == TRADE_RETCODE_LOCKED);
        if(!retryable || retry >= 3) break;
        if(InpDebugMode) Print("PositionModify 重试 #", ticket, " retcode=", retcode, " ", trade.ResultRetcodeDescription());
        Sleep(100);
    }
    if(InpDebugMode) Print("PositionModify 失败 #", ticket, " retcode=", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
    return false;
}

// 记录下单失败详情(挂单价/止损价/当前买卖价)供回测分析
void LogTradeFailure(string signalName, string side, double orderPrice, double brokerSL, double tp)
{
    double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    uint retcode = trade.ResultRetcode();
    string errDesc = trade.ResultRetcodeDescription();
    if(InpDebugMode)
        Print("下单失败: ", signalName, " ", side, " | ", errDesc, " (retcode=", retcode, ") | ",
              "挂单/市价=", DoubleToString(orderPrice, g_SymbolDigits), " SL=", DoubleToString(brokerSL, g_SymbolDigits),
              " TP=", DoubleToString(tp, g_SymbolDigits), " | Ask=", DoubleToString(ask, g_SymbolDigits),
              " Bid=", DoubleToString(bid, g_SymbolDigits));
}

//+------------------------------------------------------------------+
//| Process Signal                                                    |
//+------------------------------------------------------------------+
void ProcessSignal(ENUM_SIGNAL_TYPE signal, double stopLoss, double baseHeight)
{
    string side = GetSignalSide(signal);
    if(side == "") return;
    string signalName = SignalTypeToString(signal);
    
    // Always In过滤: 顺势信号必须与AI方向一致
    bool isTrendSignal = (signal == SIGNAL_SPIKE_BUY || signal == SIGNAL_SPIKE_SELL ||
                          signal == SIGNAL_H1_BUY || signal == SIGNAL_H2_BUY ||
                          signal == SIGNAL_L1_SELL || signal == SIGNAL_L2_SELL ||
                          signal == SIGNAL_MICRO_CH_BUY || signal == SIGNAL_MICRO_CH_SELL ||
                          signal == SIGNAL_TREND_BAR_BUY || signal == SIGNAL_TREND_BAR_SELL ||
                          signal == SIGNAL_GAP_BAR_BUY || signal == SIGNAL_GAP_BAR_SELL ||
                          signal == SIGNAL_TR_BREAKOUT_BUY || signal == SIGNAL_TR_BREAKOUT_SELL ||
                          signal == SIGNAL_BO_PULLBACK_BUY || signal == SIGNAL_BO_PULLBACK_SELL);
    
    if(isTrendSignal)
    {
        if(side == "buy" && g_AlwaysIn == AI_SHORT) return;
        if(side == "sell" && g_AlwaysIn == AI_LONG) return;
    }
    
    // 点差过滤
    if(InpEnableSpreadFilter && g_SpreadFilterActive) return;
    
    // 反向持仓检查: 避免锁仓（已有空头时不开多，已有多头时不开空）
    if(side == "buy" && HasPositionOfType(POSITION_TYPE_SELL)) return;
    if(side == "sell" && HasPositionOfType(POSITION_TYPE_BUY)) return;
    
    ENUM_ORDER_TYPE orderType = DetermineOrderType(signal, side);
    bool isMarketOrder = (orderType == ORDER_TYPE_BUY || orderType == ORDER_TYPE_SELL);
    bool isStopOrder   = (orderType == ORDER_TYPE_BUY_STOP || orderType == ORDER_TYPE_SELL_STOP);
    
    double entryPrice = 0, stopOrderPrice = 0;
    if(isMarketOrder)
    {
        entryPrice = side == "buy" ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
        stopOrderPrice = entryPrice;  // 市价单时用于日志
    }
    else if(isStopOrder)
    {
        stopOrderPrice = CalculateStopOrderPrice(side);
        entryPrice = stopOrderPrice;
    }
    
    if(stopLoss <= 0) return;
    
    double atr = g_ATRBuffer[1];
    // Al Brooks 统一止损: 一律用最近 swing 外 + 缓冲，替代各信号自算的止损
    double technicalSL = GetBrooksStopLoss(side, entryPrice, atr);
    if(technicalSL <= 0) return;
    // 强趋势下取信号K线止损与GetBrooksStopLoss的更紧者
    if(InpUseSignalBarSLInStrongTrend && atr > 0 && stopLoss > 0)
    {
        bool isStrongTrend = (g_MarketState == MARKET_STATE_STRONG_TREND ||
                             g_MarketState == MARKET_STATE_BREAKOUT ||
                             g_MarketState == MARKET_STATE_TIGHT_CHANNEL);
        if(isStrongTrend)
        {
            if(side == "buy" && stopLoss < entryPrice && (entryPrice - stopLoss) <= atr * InpMaxStopATRMult)
                technicalSL = MathMax(technicalSL, stopLoss);
            else if(side == "sell" && stopLoss > entryPrice && (stopLoss - entryPrice) <= atr * InpMaxStopATRMult)
                technicalSL = MathMin(technicalSL, stopLoss);
        }
    }
    double risk = side == "buy" ? (entryPrice - technicalSL) : (technicalSL - entryPrice);
    if(risk <= 0 || (atr > 0 && risk > atr * InpMaxStopATRMult)) return;
    
    double brokerSL = 0;
    if(InpEnableHardStop)
    {
        double extraBuffer = risk * (InpHardStopBufferMult - 1.0);
        brokerSL = side == "buy" ? technicalSL - extraBuffer : technicalSL + extraBuffer;
        brokerSL = NormalizeDouble(brokerSL, g_SymbolDigits);
    }
    
    // TP计算: Scalp TP1=1:1盈亏比; Swing TP2=测量移动(信号棒+前棒高度*200%)或通道线
    double tp1 = GetScalpTP1(side, entryPrice, technicalSL);
    double tp2 = GetMeasuredMoveTP2(side, entryPrice, atr);
    
    technicalSL = NormalizeDouble(technicalSL, g_SymbolDigits);
    tp1 = NormalizeDouble(tp1, g_SymbolDigits);
    tp2 = NormalizeDouble(tp2, g_SymbolDigits);
    entryPrice = NormalizeDouble(entryPrice, g_SymbolDigits);
    
    // Broker最小止损距离(与挂单校验共用兜底点数,防invalid stops)
    if(InpEnableHardStop && brokerSL > 0)
    {
        long stopLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
        int effectivePoints = (int)MathMax((long)InpMinStopsLevelPoints, stopLevel);
        double minDist = effectivePoints * g_SymbolPoint;
        if(MathAbs(entryPrice - brokerSL) < minDist)
        {
            brokerSL = side == "buy" ? entryPrice - minDist - g_SymbolPoint : entryPrice + minDist + g_SymbolPoint;
            brokerSL = NormalizeDouble(brokerSL, g_SymbolDigits);
        }
    }
    
    // 两笔单模式: 仓位1 TP=TP1(服务器挂单), 仓位2 TP=TP2(开仓即设)；TP1触发后仅保本，不追踪
    double volumeMin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    bool useTwoPositions = (InpTP1ClosePercent > 0 && InpTP1ClosePercent < 100);
    double lot1 = 0, lot2 = 0;
    if(useTwoPositions)
    {
        lot1 = NormalizeLot(InpLotSize * InpTP1ClosePercent / 100.0);
        lot2 = NormalizeLot(InpLotSize - lot1);
        if(lot2 < volumeMin) useTwoPositions = false;
        else if(CountPositions() + 2 > InpMaxPositions * 2) return;
    }
    if(!useTwoPositions && CountPositions() >= InpMaxPositions) return;
    
    string commentBase = signalName + "_" + TimeToString(TimeCurrent(), TIME_MINUTES);
    int magicScalp  = InpMagicNumber;      // Scalp 仓位 (TP1)
    int magicRunner = InpMagicNumber + 1;  // Runner 仓位 (TP2/Swing)
    trade.SetExpertMagicNumber(InpMagicNumber);
    
    // 方案A: 挂单前校验，不通过则跳过(不追单)
    if(isStopOrder)
    {
        double tpForValidation = useTwoPositions ? tp1 : tp2;
        string reason = "";
        if(!ValidateStopOrder(side, stopOrderPrice, brokerSL, tpForValidation, reason))
        {
            if(InpDebugMode) Print("挂单校验不通过，跳过: ", signalName, " ", reason);
            return;
        }
    }
    
    if(useTwoPositions)
    {
        const string commentScalp  = "Brooks_Scalp";
        const string commentRunner = "Brooks_Runner";
        bool ok1 = false, ok2 = false;
        ulong ticket1 = 0, ticket2 = 0;
        
        // Brooks 信号棒-入场棒 时效: 挂单仅当前K线有效,下一根K线未突破则失效
        datetime expiration = isStopOrder ? (TimeCurrent() + PeriodSeconds(PERIOD_CURRENT)) : 0;
        stopOrderPrice = NormalizeDouble(stopOrderPrice, g_SymbolDigits);
        
        ok1 = ExecuteTradeWithRetry(magicScalp, side, isStopOrder, lot1, stopOrderPrice, brokerSL,
                                   tp1, expiration, commentScalp);
        if(ok1) ticket1 = trade.ResultOrder();
        
        ok2 = ExecuteTradeWithRetry(magicRunner, side, isStopOrder, lot2, stopOrderPrice, brokerSL,
                                   tp2, expiration, isStopOrder ? commentRunner : commentRunner);
        if(ok2) ticket2 = trade.ResultOrder();
        
        if(ok1 && ok2)
        {
            if(InpEnableSoftStop)
            {
                AddSoftStopInfo(ticket1, technicalSL, side, 0);       // Scalp: 无TP1追踪
                AddSoftStopInfo(ticket2, technicalSL, side, tp1);     // Runner: TP1触及后移保本
            }
            if(isStopOrder)
            {
                AddPendingStopOrder(ticket1, technicalSL, tp1, side, signalName, commentScalp);
                AddPendingStopOrder(ticket2, technicalSL, 0, side, signalName, commentRunner);
            }
            if(InpDebugMode) Print("=== ", signalName, " ", side == "buy" ? "BUY" : "SELL", " 两笔单 | Scalp(Magic ", magicScalp, ") TP1:", DoubleToString(tp1, g_SymbolDigits), " Runner(Magic ", magicRunner, ") TP2:", DoubleToString(tp2, g_SymbolDigits), " | SL:", DoubleToString(technicalSL, g_SymbolDigits), " ===");
        }
        else
        {
            if(ok1) PositionCloseWithRetry(ticket1);
            if(ok2) PositionCloseWithRetry(ticket2);
            double logPrice = isStopOrder ? stopOrderPrice : entryPrice;
            LogTradeFailure(signalName, side, logPrice, brokerSL, tp2);
        }
        return;
    }
    
    // 单笔模式
    string comment = commentBase;
    datetime expiration = isStopOrder ? (TimeCurrent() + PeriodSeconds(PERIOD_CURRENT)) : 0;
    stopOrderPrice = NormalizeDouble(stopOrderPrice, g_SymbolDigits);
    bool result = ExecuteTradeWithRetry(InpMagicNumber, side, isStopOrder, InpLotSize,
                                      stopOrderPrice, brokerSL, tp2, expiration, comment);
    
    if(result)
    {
        ulong ticket = trade.ResultOrder();
        if(isMarketOrder)
        {
            if(InpEnableSoftStop) AddSoftStopInfo(ticket, technicalSL, side);
            AddTP1Info(ticket, tp1, side);
        }
        else if(isStopOrder)
            AddPendingStopOrder(ticket, technicalSL, tp1, side, signalName, comment);
        if(InpDebugMode) Print("=== ", signalName, " ", side == "buy" ? "BUY" : "SELL",
              " | SL:", DoubleToString(technicalSL, g_SymbolDigits),
              (" | TP1:" + DoubleToString(tp1, g_SymbolDigits) + " | TP2:" + DoubleToString(tp2, g_SymbolDigits)), " ===");
    }
    else
        LogTradeFailure(signalName, side, isStopOrder ? stopOrderPrice : entryPrice, brokerSL, tp2);
}


//+------------------------------------------------------------------+
//| Pending Stop Order Management                                     |
//+------------------------------------------------------------------+
void AddPendingStopOrder(ulong orderTicket, double technicalSL, double tp1Price, string side, string signalName, string orderComment = "")
{
    if(g_PendingStopOrderCount >= MAX_PENDING_STOP_ORDERS)
    {
        for(int i = 0; i < g_PendingStopOrderCount - 1; i++)
            g_PendingStopOrders[i] = g_PendingStopOrders[i + 1];
        g_PendingStopOrderCount--;
    }
    
    ArrayResize(g_PendingStopOrders, g_PendingStopOrderCount + 1);
    g_PendingStopOrders[g_PendingStopOrderCount].orderTicket = orderTicket;
    g_PendingStopOrders[g_PendingStopOrderCount].technicalSL = technicalSL;
    g_PendingStopOrders[g_PendingStopOrderCount].tp1Price = tp1Price;
    g_PendingStopOrders[g_PendingStopOrderCount].side = side;
    g_PendingStopOrders[g_PendingStopOrderCount].signalName = signalName;
    g_PendingStopOrders[g_PendingStopOrderCount].orderComment = orderComment;
    g_PendingStopOrderCount++;
}

// 与 CheckSoftStopExit 逻辑对齐: 新成交仓位纳入软止损时使用 g_AtrValue(实时波动率), 避免 Spike 下 techSL 过紧导致误平仓
void CheckPendingStopOrderFills()
{
    if(g_PendingStopOrderCount == 0) return;
    
    for(int i = g_PendingStopOrderCount - 1; i >= 0; i--)
    {
        if(i < 0 || i >= g_PendingStopOrderCount || i >= ArraySize(g_PendingStopOrders)) break;
        ulong orderTicket = g_PendingStopOrders[i].orderTicket;
        bool filled = false;
        ulong posTicket = 0;
        
        string matchComment = g_PendingStopOrders[i].orderComment;
        long magicScalp = InpMagicNumber, magicRunner = InpMagicNumber + 1;
        for(int p = PositionsTotal() - 1; p >= 0; p--)
        {
            if(!positionInfo.SelectByIndex(p)) continue;
            if(positionInfo.Symbol() != _Symbol) continue;
            long posMagic = positionInfo.Magic();
            if(posMagic != magicScalp && posMagic != magicRunner) continue;
            
            string posComment = positionInfo.Comment();
            bool match = (matchComment != "" && posComment == matchComment) ||
                         (matchComment == "" && StringFind(posComment, g_PendingStopOrders[i].signalName) >= 0);
            if(match)
            {
                posTicket = positionInfo.Ticket();
                bool alreadyTracked = false;
                int softListLen = ArraySize(g_SoftStopList);
                for(int s = 0; s < g_SoftStopCount && s < softListLen; s++)
                {
                    if(g_SoftStopList[s].ticket == posTicket) { alreadyTracked = true; break; }
                }
                if(!alreadyTracked) { filled = true; break; }
            }
        }
        
        if(filled && posTicket > 0)
        {
            string side = g_PendingStopOrders[i].side;
            double technicalSLToUse = g_PendingStopOrders[i].technicalSL;
            double tp1ToUse = g_PendingStopOrders[i].tp1Price;
            
            if(PositionSelectByTicket(posTicket))
            {
                double fillPrice = PositionGetDouble(POSITION_PRICE_OPEN);
                double atr = (g_AtrValue > 0) ? g_AtrValue : ((ArraySize(g_ATRBuffer) > 1) ? g_ATRBuffer[1] : 0);
                if(g_BufferSize >= 2 && atr > 0)
                {
                    double newTechnicalSL = GetBrooksStopLoss(side, fillPrice, atr);
                    double riskNew = side == "buy" ? (fillPrice - newTechnicalSL) : (newTechnicalSL - fillPrice);
                    if(newTechnicalSL > 0 && riskNew > 0 && riskNew <= atr * InpMaxStopATRMult)
                    {
                        if(side == "buy" && newTechnicalSL < fillPrice) technicalSLToUse = newTechnicalSL;
                        else if(side == "sell" && newTechnicalSL > fillPrice) technicalSLToUse = newTechnicalSL;
                    }
                }
                if(technicalSLToUse != g_PendingStopOrders[i].technicalSL && InpEnableHardStop)
                {
                    double risk = side == "buy" ? (fillPrice - technicalSLToUse) : (technicalSLToUse - fillPrice);
                    double extra = risk * (InpHardStopBufferMult - 1.0);
                    double brokerSL = side == "buy" ? NormalizeDouble(technicalSLToUse - extra, g_SymbolDigits) : NormalizeDouble(technicalSLToUse + extra, g_SymbolDigits);
                    double posTP = PositionGetDouble(POSITION_TP);
                    PositionModifyWithRetry(posTicket, brokerSL, posTP);
                }
                if(g_PendingStopOrders[i].orderComment != "Brooks_Scalp" && g_PendingStopOrders[i].orderComment != "Brooks_Runner")
                {
                    tp1ToUse = GetScalpTP1(side, fillPrice, technicalSLToUse);
                }
            }
            
            if(InpEnableSoftStop)
                AddSoftStopInfo(posTicket, technicalSLToUse, side);
            if(tp1ToUse > 0 && g_PendingStopOrders[i].orderComment != "Brooks_Scalp" && g_PendingStopOrders[i].orderComment != "Brooks_Runner")
                AddTP1Info(posTicket, tp1ToUse, side);
            
            for(int j = i; j < g_PendingStopOrderCount - 1; j++)
                g_PendingStopOrders[j] = g_PendingStopOrders[j + 1];
            g_PendingStopOrderCount--;
        }
        else
        {
            bool orderExists = false;
            for(int o = OrdersTotal() - 1; o >= 0; o--)
            {
                if(OrderGetTicket(o) == orderTicket) { orderExists = true; break; }
            }
            if(!orderExists)
            {
                for(int j = i; j < g_PendingStopOrderCount - 1; j++)
                    g_PendingStopOrders[j] = g_PendingStopOrders[j + 1];
                g_PendingStopOrderCount--;
            }
        }
    }
    
    ArrayResize(g_PendingStopOrders, g_PendingStopOrderCount > 0 ? g_PendingStopOrderCount : 1);
}

//+------------------------------------------------------------------+
//| Brooks 信号棒-入场棒 时效: 新K线时取消未触发的挂单(经纪商不支持自动过期时) |
//+------------------------------------------------------------------+
void CheckAndCancelExpiredOrders()
{
    if(g_PendingStopOrderCount == 0) return;
    
    for(int i = g_PendingStopOrderCount - 1; i >= 0; i--)
    {
        if(i < 0 || i >= g_PendingStopOrderCount || i >= ArraySize(g_PendingStopOrders)) break;
        ulong ticket = g_PendingStopOrders[i].orderTicket;
        string sigName = g_PendingStopOrders[i].signalName;
        
        bool orderExists = false;
        for(int o = OrdersTotal() - 1; o >= 0; o--)
        {
            if(OrderGetTicket(o) == ticket) { orderExists = true; break; }
        }
        
        if(orderExists)
        {
            if(trade.OrderDelete(ticket))
            {
                if(InpDebugMode) Print("Brooks时效: 取消挂单 #", ticket, " ", sigName);
            }
            else if(InpDebugMode)
                Print("OrderDelete 失败 #", ticket, " ", sigName, " retcode=", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
        }
        
        g_PendingStopOrderCount--;
        if(g_PendingStopOrderCount < 0) g_PendingStopOrderCount = 0;
        if(i < g_PendingStopOrderCount)
            g_PendingStopOrders[i] = g_PendingStopOrders[g_PendingStopOrderCount];
    }
    if(g_PendingStopOrderCount > 0)
        ArrayResize(g_PendingStopOrders, g_PendingStopOrderCount);
    else if(ArraySize(g_PendingStopOrders) > 0)
        ArrayResize(g_PendingStopOrders, 1);
}

//+------------------------------------------------------------------+
//| Soft Stop / TP1 Management                                        |
//+------------------------------------------------------------------+
void AddSoftStopInfo(ulong ticket, double technicalSL, string side, double tp1Price = 0)
{
    int arrLen = ArraySize(g_SoftStopList);
    for(int i = 0; i < g_SoftStopCount && i < arrLen; i++)
        if(g_SoftStopList[i].ticket == ticket) return;
    
    const int MAX_SOFT_STOP_RECORDS = 100;
    if(g_SoftStopCount >= MAX_SOFT_STOP_RECORDS)
    {
        SyncSoftStopList();
        if(g_SoftStopCount >= MAX_SOFT_STOP_RECORDS) return;
    }
    
    ArrayResize(g_SoftStopList, g_SoftStopCount + 1);
    g_SoftStopList[g_SoftStopCount].ticket = ticket;
    g_SoftStopList[g_SoftStopCount].technicalSL = technicalSL;
    g_SoftStopList[g_SoftStopCount].side = side;
    g_SoftStopList[g_SoftStopCount].tp1Price = tp1Price;
    g_SoftStopCount++;
}

// 收养旧版本/其他实例开的同 Magic 持仓，纳入软止损管理（仅启动后执行一次）
void AdoptExistingPositionsIfNeeded(double atr)
{
    static bool adopted = false;
    if(adopted || atr <= 0) return;
    
    long magicScalp = InpMagicNumber, magicRunner = InpMagicNumber + 1;
    for(int p = PositionsTotal() - 1; p >= 0; p--)
    {
        if(!positionInfo.SelectByIndex(p)) continue;
        if(positionInfo.Symbol() != _Symbol) continue;
        long mag = positionInfo.Magic();
        if(mag != magicScalp && mag != magicRunner) continue;
        
        ulong ticket = positionInfo.Ticket();
        bool inList = false;
        int softLen = ArraySize(g_SoftStopList);
        for(int s = 0; s < g_SoftStopCount && s < softLen; s++)
            if(g_SoftStopList[s].ticket == ticket) { inList = true; break; }
        if(inList) continue;
        
        double entryPrice = positionInfo.PriceOpen();
        double posSL      = positionInfo.StopLoss();
        string side      = (positionInfo.PositionType() == POSITION_TYPE_BUY) ? "buy" : "sell";
        double technicalSL = (side == "buy" && posSL > 0 && posSL < entryPrice) ? posSL :
                             (side == "sell" && posSL > 0 && posSL > entryPrice) ? posSL : 0;
        if(technicalSL <= 0 && atr > 0)
            technicalSL = GetBrooksStopLoss(side, entryPrice, atr);
        if(technicalSL > 0)
        {
            double tp1 = (mag == magicRunner) ? GetScalpTP1(side, entryPrice, technicalSL) : 0;
            AddSoftStopInfo(ticket, technicalSL, side, tp1);
        }
    }
    adopted = true;
}

void RemoveSoftStopInfo(ulong ticket)
{
    int arrLen = ArraySize(g_SoftStopList);
    for(int i = 0; i < g_SoftStopCount && i < arrLen; i++)
    {
        if(g_SoftStopList[i].ticket == ticket)
        {
            g_SoftStopCount--;
            if(g_SoftStopCount < 0) g_SoftStopCount = 0;
            if(i < g_SoftStopCount)
                g_SoftStopList[i] = g_SoftStopList[g_SoftStopCount];  // swap-with-last, O(1) 无 ArrayCopy
            return;
        }
    }
}

void SyncSoftStopList()
{
    ValidateSoftStopArray();
    long magicScalp = InpMagicNumber, magicRunner = InpMagicNumber + 1;
    int arrLen = ArraySize(g_SoftStopList);
    for(int i = g_SoftStopCount - 1; i >= 0; i--)
    {
        if(i < 0 || i >= g_SoftStopCount || i >= arrLen) break;
        bool exists = PositionSelectByTicket(g_SoftStopList[i].ticket);
        long mag = exists ? PositionGetInteger(POSITION_MAGIC) : 0;
        bool magicOk = exists && (mag == magicScalp || mag == magicRunner);
        if(!exists || !magicOk)
        {
            g_SoftStopCount--;
            if(g_SoftStopCount < 0) g_SoftStopCount = 0;
            if(i < g_SoftStopCount)
                g_SoftStopList[i] = g_SoftStopList[g_SoftStopCount];
        }
    }
    if(g_SoftStopCount > 0)
        ArrayResize(g_SoftStopList, g_SoftStopCount);
}

void AddTP1Info(ulong ticket, double tp1Price, string side)
{
    int tp1Len = ArraySize(g_TP1List);
    for(int i = 0; i < g_TP1Count && i < tp1Len; i++)
        if(g_TP1List[i].ticket == ticket) return;
    
    if(g_TP1Count >= MAX_TP1_RECORDS)
    {
        for(int i = 0; i < g_TP1Count - 1; i++)
            g_TP1List[i] = g_TP1List[i + 1];
        g_TP1Count--;
    }
    
    ArrayResize(g_TP1List, g_TP1Count + 1);
    g_TP1List[g_TP1Count].ticket = ticket;
    g_TP1List[g_TP1Count].tp1Price = tp1Price;
    g_TP1List[g_TP1Count].side = side;
    g_TP1Count++;
}

void RemoveTP1Info(ulong ticket)
{
    int tp1Len = ArraySize(g_TP1List);
    for(int i = 0; i < g_TP1Count && i < tp1Len; i++)
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

double GetTP1Price(ulong ticket)
{
    int tp1Len = ArraySize(g_TP1List);
    for(int i = 0; i < g_TP1Count && i < tp1Len; i++)
        if(g_TP1List[i].ticket == ticket) return g_TP1List[i].tp1Price;
    return 0;
}

string GetTP1Side(ulong ticket)
{
    int tp1Len = ArraySize(g_TP1List);
    for(int i = 0; i < g_TP1Count && i < tp1Len; i++)
        if(g_TP1List[i].ticket == ticket) return g_TP1List[i].side;
    return "";
}

void SyncTP1List()
{
    for(int i = g_TP1Count - 1; i >= 0; i--)
    {
        if(i < 0 || i >= g_TP1Count || i >= ArraySize(g_TP1List)) break;
        if(!PositionSelectByTicket(g_TP1List[i].ticket))
        {
            for(int j = i; j < g_TP1Count - 1; j++)
                g_TP1List[j] = g_TP1List[j + 1];
            g_TP1Count--;
        }
    }
    ArrayResize(g_TP1List, g_TP1Count > 0 ? g_TP1Count : 1);
}

// 保本距离: ATR倍数>0时用0.1*ATR，否则用固定点数
double GetBreakevenDistance(double atr)
{
    if(atr > 0 && InpBreakevenATRMult > 0)
        return atr * InpBreakevenATRMult;
    int pts = (InpBreakevenPoints > 0) ? InpBreakevenPoints : 5;
    return pts * g_SymbolPoint;
}

// 防御性检查: 极速行情下防止 count/数组不同步导致越界
void ValidateSoftStopArray()
{
    if(g_SoftStopCount < 0) g_SoftStopCount = 0;
    int arrLen = ArraySize(g_SoftStopList);
    if(arrLen > 0 && g_SoftStopCount > arrLen)
        g_SoftStopCount = arrLen;
}

//+------------------------------------------------------------------+
//| Soft Stop Exit Check                                              |
//+------------------------------------------------------------------+
void CheckSoftStopExit()
{
    if(!InpEnableSoftStop || g_SoftStopCount == 0) return;
    ValidateSoftStopArray();
    if(g_SoftStopCount == 0) return;
    
    static int syncCounter = 0;
    if(++syncCounter >= 10) { SyncSoftStopList(); syncCounter = 0; }
    ValidateSoftStopArray();
    if(g_SoftStopCount == 0) return;
    
    if(g_BufferSize < 2) return;
    
    int currentBar = Bars(_Symbol, PERIOD_CURRENT);
    static int s_lastTrailBar = -1;
    bool doTrail = !InpTrailStopOnNewBarOnly || (currentBar != s_lastTrailBar);
    if(doTrail) s_lastTrailBar = currentBar;
    
    double prevClose = g_CloseBuffer[1];
    double prevOpen  = g_OpenBuffer[1];
    
    for(int i = g_SoftStopCount - 1; i >= 0; i--)
    {
        int arrLen = ArraySize(g_SoftStopList);
        if(i < 0 || i >= g_SoftStopCount || i >= arrLen) break;  // 防止 count 与数组不同步或循环内 Remove 导致越界
        ulong ticket = g_SoftStopList[i].ticket;
        double techSL = g_SoftStopList[i].technicalSL;
        string side   = g_SoftStopList[i].side;
        
        if(!PositionSelectByTicket(ticket))
        {
            RemoveSoftStopInfo(ticket);
            continue;
        }
        
        long posMagic = PositionGetInteger(POSITION_MAGIC);
        if(posMagic != InpMagicNumber && posMagic != InpMagicNumber + 1)
        {
            RemoveSoftStopInfo(ticket);
            continue;
        }
        
        double entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
        double atr        = (g_AtrValue > 0) ? g_AtrValue : ((ArraySize(g_ATRBuffer) > 1) ? g_ATRBuffer[1] : 0);
        long magicScalp  = InpMagicNumber;
        long magicRunner = InpMagicNumber + 1;
        
        // 结构跟踪: 仅 Runner 且仅当 M5 新 Higher Low/Lower High 时上移；Scalp 不移动止损
        if(doTrail && atr > 0 && posMagic == magicRunner)
        {
            double newSL = 0;
            if(side == "buy")
                newSL = GetM5StructuralStopForBuy(entryPrice, techSL, atr);
            else
                newSL = GetM5StructuralStopForSell(entryPrice, techSL, atr);
            if(newSL > 0)
            {
                if(side == "buy" && newSL > techSL && newSL < entryPrice)
                {
                    g_SoftStopList[i].technicalSL = newSL;
                    techSL = newSL;
                    if(InpEnableHardStop)
                    {
                        double risk   = entryPrice - newSL;
                        double extra  = risk * (InpHardStopBufferMult - 1.0);
                        double brokerSL = NormalizeDouble(newSL - extra, g_SymbolDigits);
                        double posTP   = PositionGetDouble(POSITION_TP);
                        if(PositionModifyWithRetry(ticket, brokerSL, posTP) && InpDebugMode)
                            Print("Runner 结构跟踪 #", ticket, " SL上移至 ", DoubleToString(newSL, g_SymbolDigits));
                    }
                }
                else if(side == "sell" && newSL < techSL && newSL > entryPrice)
                {
                    g_SoftStopList[i].technicalSL = newSL;
                    techSL = newSL;
                    if(InpEnableHardStop)
                    {
                        double risk   = newSL - entryPrice;
                        double extra  = risk * (InpHardStopBufferMult - 1.0);
                        double brokerSL = NormalizeDouble(newSL + extra, g_SymbolDigits);
                        double posTP   = PositionGetDouble(POSITION_TP);
                        if(PositionModifyWithRetry(ticket, brokerSL, posTP) && InpDebugMode)
                            Print("Runner 结构跟踪 #", ticket, " SL下移至 ", DoubleToString(newSL, g_SymbolDigits));
                    }
                }
            }
        }
        
        bool shouldClose = false;
        if(InpSoftStopConfirmMode == 0)
        {
            if(side == "buy" && prevClose < techSL) shouldClose = true;
            else if(side == "sell" && prevClose > techSL) shouldClose = true;
        }
        else if(InpSoftStopConfirmMode == 1)
        {
            double bodyLow  = MathMin(prevOpen, prevClose);
            double bodyHigh = MathMax(prevOpen, prevClose);
            if(side == "buy" && bodyLow < techSL) shouldClose = true;
            else if(side == "sell" && bodyHigh > techSL) shouldClose = true;
        }
        else if(InpSoftStopConfirmMode == 2 && InpSoftStopConfirmBars > 0)
        {
            int need = MathMin(InpSoftStopConfirmBars, g_BufferSize - 1);
            bool allBreak = (need > 0);
            for(int j = 1; j <= need && j < g_BufferSize && allBreak; j++)
            {
                if(side == "buy" && g_CloseBuffer[j] >= techSL) allBreak = false;
                else if(side == "sell" && g_CloseBuffer[j] <= techSL) allBreak = false;
            }
            if(need > 0 && allBreak) shouldClose = true;
        }
        
        if(shouldClose)
        {
            if(PositionCloseWithRetry(ticket))
            {
                if(InpDebugMode) Print("逻辑止损触发 #", ticket, " 技术SL:", DoubleToString(techSL, g_SymbolDigits));
                RemoveSoftStopInfo(ticket);
            }
            else
            {
                if(!PositionSelectByTicket(ticket))
                    RemoveSoftStopInfo(ticket);
            }
        }
    }
}


//+------------------------------------------------------------------+
//| Barb Wire Detection - Brooks: 连续doji/小K线区域                   |
//| Barb Wire = 连续3+根小实体K线,通常伴随重叠和doji                    |
//| 在Barb Wire区域应避免交易,等待突破                                  |
//+------------------------------------------------------------------+
void UpdateBarbWireDetection(double atr)
{
    if(!InpEnableBarbWireFilter || atr <= 0 || g_BufferSize < InpBarbWireMinBars + 1)
    {
        g_InBarbWire = false;
        return;
    }
    
    int smallBarCount = 0;
    int dojiCount = 0;
    int overlapCount = 0;
    double rangeHigh = g_HighBuffer[1];
    double rangeLow = g_LowBuffer[1];
    
    // 检查最近N根K线是否构成Barb Wire
    int checkBars = InpBarbWireMinBars + 2;
    for(int i = 1; i <= checkBars && i < g_BufferSize; i++)
    {
        double high = g_HighBuffer[i];
        double low = g_LowBuffer[i];
        double open = g_OpenBuffer[i];
        double close = g_CloseBuffer[i];
        double range = high - low;
        double body = MathAbs(close - open);
        
        if(range <= 0) continue;
        
        // 更新区域高低点
        if(high > rangeHigh) rangeHigh = high;
        if(low < rangeLow) rangeLow = low;
        
        // 小K线判定: range小于ATR阈值 或 实体占比小
        bool isSmallBar = (range < atr * InpBarbWireRangeRatio) || 
                          (body / range < InpBarbWireBodyRatio);
        
        // Doji判定: 实体极小
        bool isDoji = (body / range < 0.15);
        
        if(isSmallBar) smallBarCount++;
        if(isDoji) dojiCount++;
        
        // 检查与前一根K线的重叠程度 - Brooks强调的关键特征
        if(i > 1 && i < g_BufferSize)
        {
            double prevHigh = g_HighBuffer[i-1];
            double prevLow = g_LowBuffer[i-1];
            double overlapHigh = MathMin(high, prevHigh);
            double overlapLow = MathMax(low, prevLow);
            double overlapSize = overlapHigh - overlapLow;
            
            // 如果重叠超过当前K线range的50%,算作高重叠
            if(overlapSize > 0 && range > 0 && overlapSize / range > 0.5)
                overlapCount++;
        }
    }
    
    // Barb Wire条件: 连续小K线 + 至少1个doji + K线高度重叠
    double totalRange = rangeHigh - rangeLow;
    bool highOverlap = (totalRange < atr * 1.5) || (overlapCount >= InpBarbWireMinBars - 1);
    
    if(smallBarCount >= InpBarbWireMinBars && dojiCount >= 1 && highOverlap)
    {
        if(!g_InBarbWire)
        {
            g_InBarbWire = true;
            g_BarbWireBarCount = 0;
            g_BarbWireHigh = rangeHigh;
            g_BarbWireLow = rangeLow;
            if(InpEnableVerboseLog)
                if(InpDebugMode) Print("进入Barb Wire区域: High=", DoubleToString(rangeHigh, g_SymbolDigits), 
                      " Low=", DoubleToString(rangeLow, g_SymbolDigits));
        }
        g_BarbWireBarCount++;
        
        // 更新Barb Wire边界(只扩展,不收缩)
        if(g_HighBuffer[1] > g_BarbWireHigh) g_BarbWireHigh = g_HighBuffer[1];
        if(g_LowBuffer[1] < g_BarbWireLow) g_BarbWireLow = g_LowBuffer[1];
    }
    else
    {
        // 检查是否突破Barb Wire
        if(g_InBarbWire)
        {
            double currClose = g_CloseBuffer[1];
            double currRange = g_HighBuffer[1] - g_LowBuffer[1];
            double currBody = MathAbs(g_CloseBuffer[1] - g_OpenBuffer[1]);
            
            // 突破条件: 强势K线(大实体)收盘在Barb Wire区域外
            bool isStrongBar = (currRange > atr * 0.5) && (currRange > 0 && currBody / currRange > 0.5);
            bool breakoutUp = (currClose > g_BarbWireHigh && isStrongBar && g_CloseBuffer[1] > g_OpenBuffer[1]);
            bool breakoutDown = (currClose < g_BarbWireLow && isStrongBar && g_CloseBuffer[1] < g_OpenBuffer[1]);
            
            if(breakoutUp || breakoutDown)
            {
                if(InpEnableVerboseLog)
                    if(InpDebugMode) Print("Barb Wire突破: ", breakoutUp ? "向上" : "向下");
                
                // 突破Barb Wire可能触发Breakout Mode
                if(InpEnableBreakoutMode)
                {
                    g_InBreakoutMode = true;
                    g_BreakoutModeDir = breakoutUp ? "up" : "down";
                    g_BreakoutModeBarCount = 0;
                    g_BreakoutModeEntry = currClose;
                    g_BreakoutModeExtreme = breakoutUp ? g_HighBuffer[1] : g_LowBuffer[1];
                }
                
                // 重置Barb Wire状态
                g_InBarbWire = false;
                g_BarbWireBarCount = 0;
                g_BarbWireHigh = 0;
                g_BarbWireLow = 0;
            }
            else
            {
                // 非强势突破,仅重置Barb Wire状态
                g_InBarbWire = false;
                g_BarbWireBarCount = 0;
            }
        }
    }
}

//+------------------------------------------------------------------+
//| Measuring Gap Detection - Brooks: 突破缺口,趋势中点标志             |
//| Measuring Gap = 强势突破K线与前一根K线之间的缺口                     |
//| 缺口中点通常是整个运动的中点,可用于目标计算                          |
//+------------------------------------------------------------------+
void UpdateMeasuringGap(double ema, double atr)
{
    if(!InpEnableMeasuringGap || atr <= 0 || g_BufferSize < 3)
        return;
    
    // 先更新现有Measuring Gap的bar索引(在检测新Gap之前)
    if(g_HasMeasuringGap && g_MeasuringGap.isValid)
    {
        g_MeasuringGap.barIndex++;
        
        double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
        
        // Measuring Gap失效条件: 价格回填缺口超过50%
        double gapMid = (g_MeasuringGap.gapHigh + g_MeasuringGap.gapLow) / 2.0;
        if(g_MeasuringGap.direction == "up" && currLow < gapMid)
        {
            g_MeasuringGap.isValid = false;
            if(InpEnableVerboseLog)
                if(InpDebugMode) Print("向上Measuring Gap失效: 价格回填");
        }
        if(g_MeasuringGap.direction == "down" && currHigh > gapMid)
        {
            g_MeasuringGap.isValid = false;
            if(InpEnableVerboseLog)
                if(InpDebugMode) Print("向下Measuring Gap失效: 价格回填");
        }
        
        // 超过20根K线后失效
        if(g_MeasuringGap.barIndex > 20)
        {
            g_MeasuringGap.isValid = false;
            g_HasMeasuringGap = false;
        }
        
        // 如果现有Gap仍有效,不检测新Gap
        if(g_MeasuringGap.isValid)
            return;
    }
    
    // 检测新的Measuring Gap
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currOpen = g_OpenBuffer[1], currClose = g_CloseBuffer[1];
    double prevHigh = g_HighBuffer[2], prevLow = g_LowBuffer[2];
    double currRange = currHigh - currLow;
    
    if(currRange <= 0) return;
    double currBody = MathAbs(currClose - currOpen);
    
    // 检测向上Measuring Gap
    // 条件: 当前K线低点高于前一根K线高点 + 当前K线是强势阳线
    double gapUp = currLow - prevHigh;
    if(gapUp >= atr * InpMeasuringGapMinSize && currClose > currOpen && currBody / currRange > 0.5)
    {
        g_HasMeasuringGap = true;
        g_MeasuringGap.gapHigh = currLow;
        g_MeasuringGap.gapLow = prevHigh;
        g_MeasuringGap.direction = "up";
        g_MeasuringGap.barIndex = 0;  // 当前K线是Gap K线
        g_MeasuringGap.isValid = true;
        if(InpEnableVerboseLog)
            if(InpDebugMode) Print("检测到向上Measuring Gap: ", DoubleToString(prevHigh, g_SymbolDigits), 
                  " - ", DoubleToString(currLow, g_SymbolDigits));
        return;
    }
    
    // 检测向下Measuring Gap
    double gapDown = prevLow - currHigh;
    if(gapDown >= atr * InpMeasuringGapMinSize && currClose < currOpen && currBody / currRange > 0.5)
    {
        g_HasMeasuringGap = true;
        g_MeasuringGap.gapHigh = prevLow;
        g_MeasuringGap.gapLow = currHigh;
        g_MeasuringGap.direction = "down";
        g_MeasuringGap.barIndex = 0;
        g_MeasuringGap.isValid = true;
        if(InpEnableVerboseLog)
            if(InpDebugMode) Print("检测到向下Measuring Gap: ", DoubleToString(currHigh, g_SymbolDigits), 
                  " - ", DoubleToString(prevLow, g_SymbolDigits));
    }
}

//+------------------------------------------------------------------+
//| Breakout Mode - Brooks: 突破后的特殊交易模式                        |
//| 突破后市场进入特殊状态,应优先顺势交易,避免逆势                       |
//| 特点: 回调浅、持续时间短、应积极追涨/追跌                            |
//+------------------------------------------------------------------+
void UpdateBreakoutMode(double ema, double atr)
{
    if(!InpEnableBreakoutMode || atr <= 0)
        return;
    
    // 检测新的突破进入Breakout Mode
    if(!g_InBreakoutMode)
    {
        double currRange = g_HighBuffer[1] - g_LowBuffer[1];
        double currBody = MathAbs(g_CloseBuffer[1] - g_OpenBuffer[1]);
        double currClose = g_CloseBuffer[1], currOpen = g_OpenBuffer[1];
        
        // 突破K线条件: 大range + 大实体 + 收盘在极端位置
        if(currRange >= atr * InpBreakoutModeATRMult && currBody / currRange > 0.6)
        {
            bool isBullBreakout = (currClose > currOpen) && 
                                  ((currClose - g_LowBuffer[1]) / currRange > 0.75);
            bool isBearBreakout = (currClose < currOpen) && 
                                  ((g_HighBuffer[1] - currClose) / currRange > 0.75);
            
            // 额外验证: 突破前一个swing point
            if(isBullBreakout)
            {
                double recentHigh = GetRecentSwingHigh(1);
                if(recentHigh > 0 && currClose > recentHigh)
                {
                    g_InBreakoutMode = true;
                    g_BreakoutModeDir = "up";
                    g_BreakoutModeBarCount = 0;
                    g_BreakoutModeEntry = currClose;
                    g_BreakoutModeExtreme = g_HighBuffer[1];
                    if(InpEnableVerboseLog)
                        if(InpDebugMode) Print("进入Breakout Mode: 向上突破");
                }
            }
            else if(isBearBreakout)
            {
                double recentLow = GetRecentSwingLow(1);
                if(recentLow > 0 && currClose < recentLow)
                {
                    g_InBreakoutMode = true;
                    g_BreakoutModeDir = "down";
                    g_BreakoutModeBarCount = 0;
                    g_BreakoutModeEntry = currClose;
                    g_BreakoutModeExtreme = g_LowBuffer[1];
                    if(InpEnableVerboseLog)
                        if(InpDebugMode) Print("进入Breakout Mode: 向下突破");
                }
            }
        }
    }
    else
    {
        // 更新Breakout Mode状态
        g_BreakoutModeBarCount++;
        
        // 更新极值
        if(g_BreakoutModeDir == "up" && g_HighBuffer[1] > g_BreakoutModeExtreme)
        { g_BreakoutModeExtreme = g_HighBuffer[1]; }
        if(g_BreakoutModeDir == "down" && g_LowBuffer[1] < g_BreakoutModeExtreme)
        { g_BreakoutModeExtreme = g_LowBuffer[1]; }
        
        // Breakout Mode退出条件
        bool shouldExit = false;
        
        // 1. 超过指定K线数
        if(g_BreakoutModeBarCount > InpBreakoutModeBars)
            shouldExit = true;
        
        // 2. 出现反向强势K线
        double currBody = MathAbs(g_CloseBuffer[1] - g_OpenBuffer[1]);
        double currRange = g_HighBuffer[1] - g_LowBuffer[1];
        if(currRange > 0 && currBody / currRange > 0.6)
        {
            if(g_BreakoutModeDir == "up" && g_CloseBuffer[1] < g_OpenBuffer[1])
                shouldExit = true;
            if(g_BreakoutModeDir == "down" && g_CloseBuffer[1] > g_OpenBuffer[1])
                shouldExit = true;
        }
        
        // 3. 价格回撤超过50%
        double retracement = 0;
        double moveRange = 0;
        if(g_BreakoutModeDir == "up")
        {
            moveRange = g_BreakoutModeExtreme - g_BreakoutModeEntry;
            if(moveRange > atr * 0.1)  // 确保有足够的运动空间
                retracement = (g_BreakoutModeExtreme - g_LowBuffer[1]) / moveRange;
        }
        else
        {
            moveRange = g_BreakoutModeEntry - g_BreakoutModeExtreme;
            if(moveRange > atr * 0.1)
                retracement = (g_HighBuffer[1] - g_BreakoutModeExtreme) / moveRange;
        }
        
        if(moveRange > atr * 0.1 && retracement > 0.5)
            shouldExit = true;
        
        if(shouldExit)
        {
            if(InpEnableVerboseLog)
                if(InpDebugMode) Print("退出Breakout Mode");
            g_InBreakoutMode = false;
            g_BreakoutModeDir = "";
            g_BreakoutModeBarCount = 0;
        }
    }
}

//+------------------------------------------------------------------+
//| Breakout Mode Signal - Brooks: 突破模式下的特殊信号                 |
//| 在Breakout Mode中,优先寻找顺势入场机会                              |
//| 回调即入场,不需要等待完整的H2/L2形态                                |
//+------------------------------------------------------------------+
ENUM_SIGNAL_TYPE CheckBreakoutModeSignal(double ema, double atr, double &stopLoss, double &baseHeight)
{
    if(!g_InBreakoutMode || atr <= 0)
        return SIGNAL_NONE;
    
    double currHigh = g_HighBuffer[1], currLow = g_LowBuffer[1];
    double currOpen = g_OpenBuffer[1], currClose = g_CloseBuffer[1];
    double currRange = currHigh - currLow;
    
    if(currRange <= 0) return SIGNAL_NONE;
    
    // 向上Breakout Mode: 寻找回调后的买入机会
    if(g_BreakoutModeDir == "up")
    {
        // 条件: 回调K线(阴线或小阳线) + 当前K线是阳线确认
        double prevClose = g_CloseBuffer[2], prevOpen = g_OpenBuffer[2];
        bool wasPullback = (prevClose <= prevOpen) || 
                           (prevClose > prevOpen && (prevClose - prevOpen) < atr * 0.3);
        
        // 当前K线是阳线,收盘在上半部
        bool isBullConfirm = (currClose > currOpen) && 
                             ((currClose - currLow) / currRange > 0.6);
        
        if(wasPullback && isBullConfirm && CheckSignalCooldown("buy"))
        {
            // 止损在回调低点下方
            double pullbackLow = MathMin(g_LowBuffer[1], g_LowBuffer[2]);
            stopLoss = pullbackLow - atr * 0.3;
            
            if((currClose - stopLoss) <= atr * InpMaxStopATRMult)
            {
                baseHeight = g_BreakoutModeExtreme - pullbackLow;
                UpdateSignalCooldown("buy");
                return SIGNAL_BO_PULLBACK_BUY;
            }
        }
    }
    
    // 向下Breakout Mode: 寻找反弹后的卖出机会
    if(g_BreakoutModeDir == "down")
    {
        double prevClose = g_CloseBuffer[2], prevOpen = g_OpenBuffer[2];
        bool wasBounce = (prevClose >= prevOpen) || 
                         (prevClose < prevOpen && (prevOpen - prevClose) < atr * 0.3);
        
        bool isBearConfirm = (currClose < currOpen) && 
                             ((currHigh - currClose) / currRange > 0.6);
        
        if(wasBounce && isBearConfirm && CheckSignalCooldown("sell"))
        {
            double bounceHigh = MathMax(g_HighBuffer[1], g_HighBuffer[2]);
            stopLoss = bounceHigh + atr * 0.3;
            
            if((stopLoss - currClose) <= atr * InpMaxStopATRMult)
            {
                baseHeight = bounceHigh - g_BreakoutModeExtreme;
                UpdateSignalCooldown("sell");
                return SIGNAL_BO_PULLBACK_SELL;
            }
        }
    }
    
    return SIGNAL_NONE;
}

//+------------------------------------------------------------------+
//| Spread & Session Tracking                                         |
//+------------------------------------------------------------------+
void UpdateSpreadTracking()
{
    double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    g_CurrentSpread = (ask - bid) / g_SymbolPoint;
    
    if(ArraySize(g_SpreadHistory) > 0)
    {
        double oldValue = g_SpreadHistory[g_SpreadIndex];
        if(oldValue > 0) g_SpreadRunningSum -= oldValue;
        else g_SpreadValidCount++;
        
        g_SpreadHistory[g_SpreadIndex] = g_CurrentSpread;
        g_SpreadRunningSum += g_CurrentSpread;
        g_SpreadIndex = (g_SpreadIndex + 1) % InpSpreadLookback;
        
        g_AverageSpread = g_SpreadValidCount > 0 ? g_SpreadRunningSum / g_SpreadValidCount : g_CurrentSpread;
    }
    
    if(g_AverageSpread > 0 && g_CurrentSpread > g_AverageSpread * InpMaxSpreadMult)
        g_SpreadFilterActive = true;
    else
        g_SpreadFilterActive = false;
}

void UpdateSessionDetection()
{
    MqlDateTime dt;
    TimeToStruct(TimeCurrent(), dt);
    int gmtHour = (dt.hour - InpGMTOffset + 24) % 24;
    
    if(InpEnableWeekendFilter)
    {
        int day = (dt.day_of_week + 6) % 7;
        int gmtHour = (dt.hour - InpGMTOffset + 24) % 24;
        bool fridayLate = (dt.day_of_week == 5 && InpFridayCloseHour > 0 && gmtHour >= InpFridayCloseHour);
        bool weekend = (dt.day_of_week == 0 || dt.day_of_week == 6) || fridayLate;
        bool sundayBeforeOpen = (dt.day_of_week == 0 && InpMondayOpenHour > 0 && gmtHour < InpMondayOpenHour);
        g_IsWeekend = weekend || sundayBeforeOpen;
        g_IsFridayClose = (dt.day_of_week == 5 && InpFridayCloseHour > 0 && gmtHour >= InpFridayCloseHour);
    }
    else
    {
        g_IsWeekend = false;
        g_IsFridayClose = false;
    }
}

// 周一开盘大跳空时重置H/L计数(Brooks: 跳空即隐形趋势棒,已完成H1/L1)
void CheckMondayGapReset(double atr)
{
    if(atr <= 0 || InpMondayGapResetATR <= 0) return;
    MqlDateTime dt;
    TimeToStruct(TimeCurrent(), dt);
    if(dt.day_of_week != 1) return;
    datetime weekStart = iTime(_Symbol, PERIOD_CURRENT, 0);
    if(weekStart == g_MondayGapResetDone) return;
    if(g_OpenBuffer[1] == 0 || g_CloseBuffer[2] == 0) return;
    double gap = MathAbs(g_OpenBuffer[1] - g_CloseBuffer[2]);
    if(gap >= atr * InpMondayGapResetATR)
    {
        g_H_Count = 0;
        g_L_Count = 0;
        g_H_LastSwingHigh = 0;
        g_H_LastPullbackLow = 0;
        g_L_LastSwingLow = 0;
        g_L_LastBounceHigh = 0;
        g_MondayGapResetDone = weekStart;
        if(InpEnableVerboseLog)
            if(InpDebugMode) Print("周一跳空重置H/L计数 Gap=", DoubleToString(gap, g_SymbolDigits), " ATR*", InpMondayGapResetATR, "=", DoubleToString(atr*InpMondayGapResetATR, g_SymbolDigits));
    }
}

//+------------------------------------------------------------------+
//| Position Management                                               |
//+------------------------------------------------------------------+
void ManagePositions(double ema, double atr)
{
    SyncTP1List();
    int totalPositions = 0, scalpPositions = 0;
    CountPositionsBoth(totalPositions, scalpPositions);
    if(totalPositions > 0) SyncSoftStopList();
    CheckClimaxExit();
    double beDist = GetBreakevenDistance(atr);
    double beDistLarge = 2.0 * beDist;
    int magicScalp  = InpMagicNumber;
    int magicRunner = InpMagicNumber + 1;

    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        if(!positionInfo.SelectByIndex(i)) continue;
        if(positionInfo.Symbol() != _Symbol) continue;
        long magic = positionInfo.Magic();
        if(magic != magicScalp && magic != magicRunner) continue;
        
        ulong  ticket   = positionInfo.Ticket();
        double posPrice = positionInfo.PriceOpen();
        double posSL    = positionInfo.StopLoss();
        double posTP    = positionInfo.TakeProfit();
        double posVol   = positionInfo.Volume();
        long   posType  = positionInfo.PositionType();
        
        double currentPrice = posType == POSITION_TYPE_BUY ?
                              SymbolInfoDouble(_Symbol, SYMBOL_BID) :
                              SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        double risk = posType == POSITION_TYPE_BUY ? (posPrice - posSL) : (posSL - posPrice);
        
        if(risk <= 0)
        {
            if(posSL == 0 && atr > 0)
            {
                double emergSL = posType == POSITION_TYPE_BUY ? posPrice - atr * 2.0 : posPrice + atr * 2.0;
                emergSL = NormalizeDouble(emergSL, g_SymbolDigits);
                PositionModifyWithRetry(ticket, emergSL, posTP);
            }
            continue;
        }
        
        double currentRR = posType == POSITION_TYPE_BUY ?
                           (currentPrice - posPrice) / risk :
                           (posPrice - currentPrice) / risk;
        
        if(InpEnableWeekendFilter && g_IsFridayClose)
        {
            bool narrowTR = (g_MarketState == MARKET_STATE_TRADING_RANGE && g_TR_High > g_TR_Low && (g_TR_High - g_TR_Low) < atr);
            bool strongTrend = (g_AlwaysIn == AI_LONG || g_AlwaysIn == AI_SHORT) && !g_InBarbWire && !narrowTR;
            if(currentRR < InpFridayMinProfitR || !strongTrend)
            {
                PositionCloseWithRetry(ticket);
            }
            else
            {
                double beSL = (posType == POSITION_TYPE_BUY) ?
                    NormalizeDouble(posPrice + beDist, g_SymbolDigits) : NormalizeDouble(posPrice - beDist, g_SymbolDigits);
                if((posType == POSITION_TYPE_BUY && (posSL < posPrice - beDist || posSL == 0)) ||
                   (posType == POSITION_TYPE_SELL && (posSL > posPrice + beDist || posSL == 0)))
                    PositionModifyWithRetry(ticket, beSL, posTP);
            }
            continue;
        }
        
        // --- 逻辑分支 A: Scalp 单 (Magic_A = TP1) ---
        if(magic == magicScalp)
        {
            bool hasRunner = (totalPositions > scalpPositions);
            if(hasRunner)
            {
                // 两笔单模式: Scalp 不做保本，SL 由 CheckSoftStopExit 结构跟踪管理，等服务器 TP1 止盈
            }
            else
            {
                // 单笔模式: 手动 TP1 部分平仓，平仓后再移保本
                double storedTP1 = GetTP1Price(ticket);
                if(storedTP1 > 0)
                {
                    string tp1Side = GetTP1Side(ticket);
                    bool tp1Reached = tp1Side == "buy" ? (currentPrice >= storedTP1) : (currentPrice <= storedTP1);
                    bool alreadyTP1 = (posType == POSITION_TYPE_BUY && posSL >= posPrice - beDist) || (posType == POSITION_TYPE_SELL && posSL <= posPrice + beDist);
                    double volumeMin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
                    double volumeStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
                    if(tp1Reached && !alreadyTP1 && posVol > volumeMin)
                    {
                        double closeVol = NormalizeDouble(posVol * (InpTP1ClosePercent / 100.0), 2);
                        if(closeVol < volumeMin) closeVol = volumeMin;
                        if(posVol - closeVol < volumeMin) closeVol = posVol - volumeMin;
                        if(volumeStep > 0) closeVol = MathFloor(closeVol / volumeStep) * volumeStep;
                        closeVol = NormalizeDouble(closeVol, 2);
                        if(closeVol >= volumeMin && PositionClosePartialWithRetry(ticket, closeVol))
                        {
                            Sleep(100);
                            if(PositionSelectByTicket(ticket))
                            {
                                double volAfter = PositionGetDouble(POSITION_VOLUME);
                                if(volAfter < posVol - volumeMin * 0.5)
                                {
                                    double newSL = posType == POSITION_TYPE_BUY ? posPrice + beDistLarge : posPrice - beDistLarge;
                                    PositionModifyWithRetry(ticket, NormalizeDouble(newSL, g_SymbolDigits), posTP);
                                    RemoveTP1Info(ticket);
                                    if(InpDebugMode) Print("TP1部分平仓成功 #", ticket);
                                }
                            }
                        }
                    }
                }
            }
            continue;
        }
        
        // --- 逻辑分支 B: Runner 单 (Magic_B = TP2/Swing) ---
        if(magic == magicRunner)
        {
            // 0) Scalp 已止盈后: 剩余 Runner 立即移保本
            bool scalpGone = (totalPositions == 1 && scalpPositions == 0);
            if(scalpGone)
            {
                bool alreadyAtEntry = (posType == POSITION_TYPE_BUY && posSL >= posPrice - g_SymbolPoint) ||
                                       (posType == POSITION_TYPE_SELL && posSL <= posPrice + g_SymbolPoint);
                if(!alreadyAtEntry)
                {
                    double beSL = NormalizeDouble(posPrice, g_SymbolDigits);
                    if(PositionModifyWithRetry(ticket, beSL, posTP) && InpDebugMode)
                        Print("Runner 保本触发 #", ticket, " SL移至开仓价 (Scalp已止盈) ", DoubleToString(posPrice, g_SymbolDigits));
                }
            }
            // 1) 保本触发: 单笔模式或 Scalp 仍在时,只有当利润达到风险金额的 1.2 倍时,才将止损移至开仓价
            else
            {
            double riskForBE = 0;
            for(int s = 0; s < g_SoftStopCount && s < ArraySize(g_SoftStopList); s++)
                if(g_SoftStopList[s].ticket == ticket)
                {
                    riskForBE = (posType == POSITION_TYPE_BUY) ? (posPrice - g_SoftStopList[s].technicalSL) : (g_SoftStopList[s].technicalSL - posPrice);
                    break;
                }
            double profit = (posType == POSITION_TYPE_BUY) ? (currentPrice - posPrice) : (posPrice - currentPrice);
            bool profitReached12R = (riskForBE > 0 && profit >= riskForBE * 1.2);
            bool alreadyAtEntry = (posType == POSITION_TYPE_BUY && posSL >= posPrice - g_SymbolPoint) ||
                                   (posType == POSITION_TYPE_SELL && posSL <= posPrice + g_SymbolPoint);
            if(profitReached12R && !alreadyAtEntry)
            {
                double beSL = NormalizeDouble(posPrice, g_SymbolDigits);
                if(PositionModifyWithRetry(ticket, beSL, posTP) && InpDebugMode)
                    Print("Runner 保本触发 #", ticket, " SL移至开仓价 ", DoubleToString(posPrice, g_SymbolDigits));
            }
            }
            // 2) 结构跟踪由 CheckSoftStopExit 处理（仅当 M5 新 Higher Low 时上移）
        }
    }
}

// 单次遍历同时统计总仓位数和 Scalp 仓位数
void CountPositionsBoth(int &totalOut, int &scalpOut)
{
    totalOut = 0;
    scalpOut = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        if(!positionInfo.SelectByIndex(i)) continue;
        if(positionInfo.Symbol() != _Symbol) continue;
        long magic = positionInfo.Magic();
        if(magic == InpMagicNumber)
        {
            totalOut++;
            scalpOut++;
        }
        else if(magic == InpMagicNumber + 1)
            totalOut++;
    }
}

int CountPositions()
{
    int total = 0, scalp = 0;
    CountPositionsBoth(total, scalp);
    return total;
}

// 是否存在指定方向的持仓（避免锁仓）
bool HasPositionOfType(ENUM_POSITION_TYPE posType)
{
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        if(!positionInfo.SelectByIndex(i)) continue;
        if(positionInfo.Symbol() != _Symbol) continue;
        long magic = positionInfo.Magic();
        if(magic != InpMagicNumber && magic != InpMagicNumber + 1) continue;
        if(positionInfo.PositionType() == posType) return true;
    }
    return false;
}

// (SaveState/LoadState 已移除 - 无状态持久化)

//+------------------------------------------------------------------+
//| Helper Functions                                                  |
//+------------------------------------------------------------------+
string GetSignalSide(ENUM_SIGNAL_TYPE signal)
{
    switch(signal)
    {
        case SIGNAL_SPIKE_BUY:
        case SIGNAL_H1_BUY:
        case SIGNAL_H2_BUY:
        case SIGNAL_MICRO_CH_BUY:
        case SIGNAL_WEDGE_BUY:
        case SIGNAL_CLIMAX_BUY:
        case SIGNAL_MTR_BUY:
        case SIGNAL_FAILED_BO_BUY:
        case SIGNAL_FINAL_FLAG_BUY:
        case SIGNAL_DT_BUY:
        case SIGNAL_TREND_BAR_BUY:
        case SIGNAL_REV_BAR_BUY:
        case SIGNAL_II_BUY:
        case SIGNAL_OUTSIDE_BAR_BUY:
        case SIGNAL_MEASURED_MOVE_BUY:
        case SIGNAL_TR_BREAKOUT_BUY:
        case SIGNAL_BO_PULLBACK_BUY:
        case SIGNAL_GAP_BAR_BUY:
            return "buy";
            
        case SIGNAL_SPIKE_SELL:
        case SIGNAL_L1_SELL:
        case SIGNAL_L2_SELL:
        case SIGNAL_MICRO_CH_SELL:
        case SIGNAL_WEDGE_SELL:
        case SIGNAL_CLIMAX_SELL:
        case SIGNAL_MTR_SELL:
        case SIGNAL_FAILED_BO_SELL:
        case SIGNAL_FINAL_FLAG_SELL:
        case SIGNAL_DT_SELL:
        case SIGNAL_TREND_BAR_SELL:
        case SIGNAL_REV_BAR_SELL:
        case SIGNAL_II_SELL:
        case SIGNAL_OUTSIDE_BAR_SELL:
        case SIGNAL_MEASURED_MOVE_SELL:
        case SIGNAL_TR_BREAKOUT_SELL:
        case SIGNAL_BO_PULLBACK_SELL:
        case SIGNAL_GAP_BAR_SELL:
            return "sell";
            
        default: return "";
    }
}

string SignalTypeToString(ENUM_SIGNAL_TYPE signal)
{
    switch(signal)
    {
        case SIGNAL_SPIKE_BUY:           return "Spike_Buy";
        case SIGNAL_SPIKE_SELL:          return "Spike_Sell";
        case SIGNAL_H1_BUY:              return "H1_Buy";
        case SIGNAL_H2_BUY:              return "H2_Buy";
        case SIGNAL_L1_SELL:             return "L1_Sell";
        case SIGNAL_L2_SELL:             return "L2_Sell";
        case SIGNAL_MICRO_CH_BUY:        return "MicroCH_Buy";
        case SIGNAL_MICRO_CH_SELL:       return "MicroCH_Sell";
        case SIGNAL_WEDGE_BUY:           return "Wedge_Buy";
        case SIGNAL_WEDGE_SELL:          return "Wedge_Sell";
        case SIGNAL_CLIMAX_BUY:          return "Climax_Buy";
        case SIGNAL_CLIMAX_SELL:         return "Climax_Sell";
        case SIGNAL_MTR_BUY:             return "MTR_Buy";
        case SIGNAL_MTR_SELL:            return "MTR_Sell";
        case SIGNAL_FAILED_BO_BUY:       return "FailedBO_Buy";
        case SIGNAL_FAILED_BO_SELL:      return "FailedBO_Sell";
        case SIGNAL_FINAL_FLAG_BUY:      return "FinalFlag_Buy";
        case SIGNAL_FINAL_FLAG_SELL:     return "FinalFlag_Sell";
        case SIGNAL_DT_BUY:              return "DoubleBottom_Buy";
        case SIGNAL_DT_SELL:             return "DoubleTop_Sell";
        case SIGNAL_TREND_BAR_BUY:       return "TrendBar_Buy";
        case SIGNAL_TREND_BAR_SELL:      return "TrendBar_Sell";
        case SIGNAL_REV_BAR_BUY:         return "RevBar_Buy";
        case SIGNAL_REV_BAR_SELL:        return "RevBar_Sell";
        case SIGNAL_II_BUY:              return "IIPattern_Buy";
        case SIGNAL_II_SELL:             return "IIPattern_Sell";
        case SIGNAL_OUTSIDE_BAR_BUY:     return "OutsideBar_Buy";
        case SIGNAL_OUTSIDE_BAR_SELL:    return "OutsideBar_Sell";
        case SIGNAL_MEASURED_MOVE_BUY:   return "MeasuredMove_Buy";
        case SIGNAL_MEASURED_MOVE_SELL:  return "MeasuredMove_Sell";
        case SIGNAL_TR_BREAKOUT_BUY:     return "TRBreakout_Buy";
        case SIGNAL_TR_BREAKOUT_SELL:    return "TRBreakout_Sell";
        case SIGNAL_BO_PULLBACK_BUY:     return "BOPullback_Buy";
        case SIGNAL_BO_PULLBACK_SELL:    return "BOPullback_Sell";
        case SIGNAL_GAP_BAR_BUY:         return "GapBar_Buy";
        case SIGNAL_GAP_BAR_SELL:        return "GapBar_Sell";
        default:                         return "Unknown";
    }
}

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

string GetAlwaysInString(ENUM_ALWAYS_IN ai)
{
    switch(ai)
    {
        case AI_LONG:    return "AI_Long";
        case AI_SHORT:   return "AI_Short";
        case AI_NEUTRAL: return "AI_Neutral";
        default:         return "Unknown";
    }
}

void PrintBarLog(int gapCount, string extra)
{
    if(!InpDebugMode) return;
    double close = g_CloseBuffer[1];
    double open  = g_OpenBuffer[1];
    string barType = close > open ? "Bull" : (close < open ? "Bear" : "Doji");
    string specialState = "";
    if(g_InBarbWire) specialState += "[BW]";
    if(g_InBreakoutMode) specialState += "[BO:" + g_BreakoutModeDir + "]";
    if(g_HasMeasuringGap && g_MeasuringGap.isValid) specialState += "[MG]";
    Print(StringFormat("K#%d|%s|%s|%s|H:%d L:%d|Gap:%d%s",
        g_BarCount, barType, GetMarketStateString(g_MarketState), GetAlwaysInString(g_AlwaysIn),
        g_H_Count, g_L_Count, gapCount, specialState));
}

void PrintSignalLog(ENUM_SIGNAL_TYPE signal, double stopLoss, double atr)
{
    if(!InpDebugMode) return;
    string side = GetSignalSide(signal);
    double entryPrice = side == "buy" ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double risk = side == "buy" ? (entryPrice - stopLoss) : (stopLoss - entryPrice);
    double riskATR = atr > 0 ? risk / atr : 0;
    Print(StringFormat("SIGNAL: %s | Entry:%.5f | SL:%.5f | Risk:%.1fATR", SignalTypeToString(signal), entryPrice, stopLoss, riskATR));
}

//+------------------------------------------------------------------+
