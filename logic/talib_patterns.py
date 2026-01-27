"""
TA-Lib 形态识别模块 - Al Brooks PA 信号增强器

使用 TA-Lib 的 60+ 种 K 线形态作为预过滤器，
当 TA-Lib 形态与 Al Brooks PA 逻辑重合时，给信号分配更高的置信度。

Al Brooks 形态映射：
- 反转形态（Reversal）: 对应 Climax/Wedge 反转信号
- 吞没形态（Engulfing）: 对应 Failed Breakout
- 锤子/射击之星: 对应 Signal Bar 质量验证
- 十字星: 对应犹豫/反转信号
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import pandas as pd

try:
    import talib
    TALIB_AVAILABLE = True
except ImportError:
    TALIB_AVAILABLE = False
    logging.warning("⚠️ TA-Lib 未安装，形态识别功能将被禁用")


class PatternCategory(Enum):
    """形态类别"""
    BULLISH_REVERSAL = "bullish_reversal"      # 看涨反转
    BEARISH_REVERSAL = "bearish_reversal"      # 看跌反转
    BULLISH_CONTINUATION = "bullish_cont"      # 看涨延续
    BEARISH_CONTINUATION = "bearish_cont"      # 看跌延续
    INDECISION = "indecision"                  # 犹豫形态
    STRENGTH = "strength"                      # 力量形态


@dataclass
class PatternMatch:
    """形态匹配结果"""
    name: str                    # 形态名称
    category: PatternCategory    # 形态类别
    strength: int                # 强度 (-100 到 100)
    brooks_alignment: str        # 对应的 Al Brooks 概念
    confidence_boost: float      # 置信度加成 (0.0 - 0.5)


class TALibPatternDetector:
    """
    TA-Lib 形态检测器
    
    将 TA-Lib 的 K 线形态与 Al Brooks 理论对应，
    作为 PA 信号的增强器。
    """
    
    # ========== TA-Lib 形态到 Al Brooks 概念的映射 ==========
    # 格式: {talib_func_name: (PatternCategory, brooks_concept, confidence_boost)}
    
    PATTERN_MAPPING = {
        # ========== 看涨反转形态 ==========
        "CDLHAMMER": (PatternCategory.BULLISH_REVERSAL, "Signal Bar (锤子线)", 0.15),
        "CDLINVERTEDHAMMER": (PatternCategory.BULLISH_REVERSAL, "Signal Bar (倒锤子)", 0.10),
        "CDLMORNINGSTAR": (PatternCategory.BULLISH_REVERSAL, "MTR (晨星)", 0.25),
        "CDLMORNINGDOJISTAR": (PatternCategory.BULLISH_REVERSAL, "MTR (晨星十字)", 0.20),
        "CDLPIERCING": (PatternCategory.BULLISH_REVERSAL, "Failed BO (刺透)", 0.15),
        "CDLENGULFING": (PatternCategory.BULLISH_REVERSAL, "Outside Bar (吞没)", 0.20),
        "CDLHARAMI": (PatternCategory.BULLISH_REVERSAL, "Inside Bar (孕线)", 0.10),
        "CDLHARAMICROSS": (PatternCategory.BULLISH_REVERSAL, "Inside Bar (十字孕线)", 0.12),
        "CDL3WHITESOLDIERS": (PatternCategory.BULLISH_REVERSAL, "Strong Trend (三白兵)", 0.25),
        "CDLABANDONEDBABY": (PatternCategory.BULLISH_REVERSAL, "Gap Reversal (弃婴)", 0.30),
        "CDLKICKING": (PatternCategory.BULLISH_REVERSAL, "Gap BO (踢脚)", 0.25),
        "CDLTAKURI": (PatternCategory.BULLISH_REVERSAL, "Signal Bar (探底)", 0.15),
        "CDLDRAGONFLYDOJI": (PatternCategory.BULLISH_REVERSAL, "Doji (蜻蜓十字)", 0.12),
        
        # ========== 看跌反转形态 ==========
        "CDLSHOOTINGSTAR": (PatternCategory.BEARISH_REVERSAL, "Signal Bar (射击之星)", 0.15),
        "CDLHANGINGMAN": (PatternCategory.BEARISH_REVERSAL, "Signal Bar (上吊线)", 0.12),
        "CDLEVENINGSTAR": (PatternCategory.BEARISH_REVERSAL, "MTR (暮星)", 0.25),
        "CDLEVENINGDOJISTAR": (PatternCategory.BEARISH_REVERSAL, "MTR (暮星十字)", 0.20),
        "CDLDARKCLOUDCOVER": (PatternCategory.BEARISH_REVERSAL, "Failed BO (乌云盖顶)", 0.15),
        "CDL3BLACKCROWS": (PatternCategory.BEARISH_REVERSAL, "Strong Trend (三黑鸦)", 0.25),
        "CDLGRAVESTONEDOJI": (PatternCategory.BEARISH_REVERSAL, "Doji (墓碑十字)", 0.12),
        "CDL2CROWS": (PatternCategory.BEARISH_REVERSAL, "Exhaustion (双鸦)", 0.15),
        "CDLADVANCEBLOCK": (PatternCategory.BEARISH_REVERSAL, "Climax (前进受阻)", 0.18),
        
        # ========== 延续形态 ==========
        "CDLRISEFALL3METHODS": (PatternCategory.BULLISH_CONTINUATION, "Pullback (上升三法)", 0.15),
        "CDL3LINESTRIKE": (PatternCategory.BULLISH_CONTINUATION, "With Trend (三线打击)", 0.12),
        "CDLSEPARATINGLINES": (PatternCategory.BULLISH_CONTINUATION, "Gap (分离线)", 0.10),
        "CDLGAPSIDESIDEWHITE": (PatternCategory.BULLISH_CONTINUATION, "Gap (缺口并列)", 0.10),
        "CDLMATHOLD": (PatternCategory.BULLISH_CONTINUATION, "Pullback (铺垫)", 0.15),
        
        # ========== 犹豫形态 ==========
        "CDLDOJI": (PatternCategory.INDECISION, "Doji (十字星)", 0.08),
        "CDLLONGLEGGEDDOJI": (PatternCategory.INDECISION, "Doji (长腿十字)", 0.10),
        "CDLSPINNINGTOP": (PatternCategory.INDECISION, "TR Bar (纺锤)", 0.05),
        "CDLHIGHWAVE": (PatternCategory.INDECISION, "TR Bar (高浪)", 0.08),
        "CDLRICKSHAWMAN": (PatternCategory.INDECISION, "TR Bar (黄包车夫)", 0.08),
        
        # ========== 力量形态 ==========
        "CDLMARUBOZU": (PatternCategory.STRENGTH, "Strong Bar (光头光脚)", 0.20),
        "CDLCLOSINGMARUBOZU": (PatternCategory.STRENGTH, "Strong Close (收盘光头)", 0.15),
        "CDLBELTHOLD": (PatternCategory.STRENGTH, "Strong Open (捉腰带)", 0.12),
        "CDLLONGLINE": (PatternCategory.STRENGTH, "Strong Bar (长实体)", 0.15),
        
        # ========== 特殊形态 ==========
        "CDLBREAKAWAY": (PatternCategory.BULLISH_REVERSAL, "Breakout (突破)", 0.18),
        "CDLCONCEALBABYSWALL": (PatternCategory.BULLISH_REVERSAL, "Trap (藏婴吞没)", 0.15),
        "CDLCOUNTERATTACK": (PatternCategory.BULLISH_REVERSAL, "Failed BO (反击线)", 0.12),
        "CDLIDENTICAL3CROWS": (PatternCategory.BEARISH_REVERSAL, "Climax (同值三鸦)", 0.20),
        "CDLINNECK": (PatternCategory.BEARISH_CONTINUATION, "Weak Pullback (颈内线)", 0.08),
        "CDLONNECK": (PatternCategory.BEARISH_CONTINUATION, "Weak Pullback (颈上线)", 0.08),
        "CDLSTALLEDPATTERN": (PatternCategory.BEARISH_REVERSAL, "Exhaustion (停顿)", 0.12),
        "CDLTHRUSTING": (PatternCategory.BEARISH_CONTINUATION, "Weak Rally (插入线)", 0.08),
        "CDLTRISTAR": (PatternCategory.BULLISH_REVERSAL, "Triple Doji (三星)", 0.20),
        "CDLUNIQUE3RIVER": (PatternCategory.BULLISH_REVERSAL, "Bottom (独特三河)", 0.15),
        "CDLUPSIDEGAP2CROWS": (PatternCategory.BEARISH_REVERSAL, "Trap (上升缺口双鸦)", 0.15),
        "CDLXSIDEGAP3METHODS": (PatternCategory.BULLISH_CONTINUATION, "Gap Continuation (缺口三法)", 0.12),
    }
    
    # Al Brooks 信号与推荐形态的对应关系
    SIGNAL_PATTERN_ALIGNMENT = {
        # 反转信号需要反转形态
        "ClimaxReversal_Buy": [PatternCategory.BULLISH_REVERSAL],
        "ClimaxReversal_Sell": [PatternCategory.BEARISH_REVERSAL],
        "WedgeReversal_Buy": [PatternCategory.BULLISH_REVERSAL],
        "WedgeReversal_Sell": [PatternCategory.BEARISH_REVERSAL],
        "FailedBreakout_Buy": [PatternCategory.BULLISH_REVERSAL],
        "FailedBreakout_Sell": [PatternCategory.BEARISH_REVERSAL],
        
        # H2/L2 回调信号需要延续形态
        "H2_Buy": [PatternCategory.BULLISH_REVERSAL, PatternCategory.BULLISH_CONTINUATION],
        "H1_Buy": [PatternCategory.BULLISH_REVERSAL, PatternCategory.BULLISH_CONTINUATION],
        "L2_Sell": [PatternCategory.BEARISH_REVERSAL, PatternCategory.BEARISH_CONTINUATION],
        "L1_Sell": [PatternCategory.BEARISH_REVERSAL, PatternCategory.BEARISH_CONTINUATION],
        
        # Spike 顺势信号需要力量形态
        "StrongSpike_Buy": [PatternCategory.STRENGTH, PatternCategory.BULLISH_CONTINUATION],
        "StrongSpike_Sell": [PatternCategory.STRENGTH, PatternCategory.BEARISH_CONTINUATION],
    }
    
    def __init__(self):
        """初始化形态检测器"""
        if not TALIB_AVAILABLE:
            logging.warning("TA-Lib 不可用，形态增强功能将被禁用")
            self._pattern_functions = {}
            return
        
        # 获取所有可用的形态函数
        self._pattern_functions = {}
        for name in self.PATTERN_MAPPING.keys():
            if hasattr(talib, name):
                self._pattern_functions[name] = getattr(talib, name)
            else:
                logging.debug(f"TA-Lib 函数 {name} 不可用")
        
        # OHLC 数据缓存（避免重复转换）
        self._cached_ohlc: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None
        self._cached_df_len: int = 0
        
        logging.info(f"📊 TA-Lib 形态检测器初始化: {len(self._pattern_functions)} 个形态可用")
    
    def detect_patterns(
        self, 
        df: pd.DataFrame, 
        lookback: int = 5
    ) -> Dict[str, List[PatternMatch]]:
        """
        检测最近 K 线的所有形态
        
        Args:
            df: K 线数据 (需要 open, high, low, close 列)
            lookback: 检测最近多少根 K 线
        
        Returns:
            Dict[str, List[PatternMatch]]: {bar_index: [形态列表]}
        """
        if not TALIB_AVAILABLE or len(df) < 10:
            return {}
        
        results: Dict[str, List[PatternMatch]] = {}
        
        # 使用缓存的 OHLC 数据（避免重复转换）
        df_len = len(df)
        if self._cached_df_len != df_len or self._cached_ohlc is None:
            self._cached_ohlc = (
                df["open"].values.astype(np.float64),
                df["high"].values.astype(np.float64),
                df["low"].values.astype(np.float64),
                df["close"].values.astype(np.float64),
            )
            self._cached_df_len = df_len
        
        open_prices, high_prices, low_prices, close_prices = self._cached_ohlc
        
        # 检测每个形态
        for func_name, func in self._pattern_functions.items():
            try:
                # 调用 TA-Lib 形态函数
                pattern_result = func(open_prices, high_prices, low_prices, close_prices)
                
                # 检查最近 lookback 根 K 线
                for i in range(-lookback, 0):
                    idx = len(df) + i
                    if idx < 0:
                        continue
                    
                    value = pattern_result[idx]
                    if value != 0:  # 非零表示检测到形态
                        category, brooks_concept, boost = self.PATTERN_MAPPING[func_name]
                        
                        # 根据值的正负判断方向
                        # 正值=看涨，负值=看跌
                        if value > 0 and category in [PatternCategory.BEARISH_REVERSAL, PatternCategory.BEARISH_CONTINUATION]:
                            # 跳过方向不匹配的
                            continue
                        if value < 0 and category in [PatternCategory.BULLISH_REVERSAL, PatternCategory.BULLISH_CONTINUATION]:
                            continue
                        
                        match = PatternMatch(
                            name=func_name.replace("CDL", ""),
                            category=category,
                            strength=int(value),
                            brooks_alignment=brooks_concept,
                            confidence_boost=boost,
                        )
                        
                        key = str(idx)
                        if key not in results:
                            results[key] = []
                        results[key].append(match)
                        
            except Exception as e:
                logging.debug(f"形态 {func_name} 检测失败: {e}")
        
        return results
    
    def detect_current_bar_patterns(self, df: pd.DataFrame) -> List[PatternMatch]:
        """
        检测当前 K 线（最后一根）的所有形态
        
        Args:
            df: K 线数据
        
        Returns:
            List[PatternMatch]: 检测到的形态列表
        """
        patterns = self.detect_patterns(df, lookback=1)
        last_idx = str(len(df) - 1)
        return patterns.get(last_idx, [])
    
    def calculate_signal_boost(
        self, 
        signal_type: str, 
        patterns: List[PatternMatch]
    ) -> Tuple[float, List[str]]:
        """
        计算信号的置信度加成
        
        当 TA-Lib 形态与 Al Brooks 信号方向一致时，
        给予置信度加成。
        
        Args:
            signal_type: 信号类型 (如 "H2_Buy", "ClimaxReversal_Sell")
            patterns: 检测到的形态列表
        
        Returns:
            (total_boost, aligned_pattern_names): 总加成和对齐的形态名称
        """
        if not patterns:
            return (0.0, [])
        
        # 获取该信号推荐的形态类别
        recommended_categories = self.SIGNAL_PATTERN_ALIGNMENT.get(signal_type, [])
        
        if not recommended_categories:
            # 根据信号名称推断
            if "Buy" in signal_type:
                recommended_categories = [PatternCategory.BULLISH_REVERSAL, PatternCategory.BULLISH_CONTINUATION]
            elif "Sell" in signal_type:
                recommended_categories = [PatternCategory.BEARISH_REVERSAL, PatternCategory.BEARISH_CONTINUATION]
            else:
                return (0.0, [])
        
        total_boost = 0.0
        aligned_names = []
        
        for pattern in patterns:
            if pattern.category in recommended_categories:
                total_boost += pattern.confidence_boost
                aligned_names.append(f"{pattern.name}({pattern.brooks_alignment})")
            elif pattern.category == PatternCategory.STRENGTH:
                # 力量形态对任何方向都有加成
                total_boost += pattern.confidence_boost * 0.5
                aligned_names.append(f"{pattern.name}(力量)")
        
        # 上限 0.5
        total_boost = min(total_boost, 0.5)
        
        return (total_boost, aligned_names)
    
    def get_pattern_summary(self, patterns: List[PatternMatch]) -> str:
        """
        获取形态摘要字符串
        
        Args:
            patterns: 形态列表
        
        Returns:
            str: 摘要字符串
        """
        if not patterns:
            return "无形态"
        
        # 按类别分组
        by_category = {}
        for p in patterns:
            cat = p.category.value
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(p.name)
        
        parts = []
        for cat, names in by_category.items():
            parts.append(f"{cat}: {', '.join(names)}")
        
        return " | ".join(parts)


# 全局单例
_talib_detector: Optional[TALibPatternDetector] = None


def get_talib_detector() -> TALibPatternDetector:
    """获取 TA-Lib 形态检测器单例"""
    global _talib_detector
    if _talib_detector is None:
        _talib_detector = TALibPatternDetector()
    return _talib_detector


def calculate_talib_boost(
    df: pd.DataFrame, 
    signal_type: str
) -> Tuple[float, List[str]]:
    """
    计算 TA-Lib 形态对信号的置信度加成
    
    Args:
        df: K 线数据
        signal_type: 信号类型
    
    Returns:
        (boost, pattern_names): 置信度加成和匹配的形态名称
    """
    if not TALIB_AVAILABLE:
        return (0.0, [])
    
    detector = get_talib_detector()
    patterns = detector.detect_current_bar_patterns(df)
    return detector.calculate_signal_boost(signal_type, patterns)
