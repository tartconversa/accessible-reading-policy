"""标准库实现的统计原语。

刻意不引入 numpy/scipy：方法与限制在 report.method_card 中随结果保留。
- 配对 t 检验：使用正则化不完全 Beta 函数给出精确 p 值。
- 置信区间：以家庭为聚类单位、按抽样权重加权的百分位自助法。
- 顺序/残留模型：高斯消元最小二乘 + t 分布检验。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

DEFAULT_BOOTSTRAP_REPS = 2000
DEFAULT_SEED = 20260401


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def weighted_mean(pairs):
    """pairs 为 (取值, 权重) 序列；权重是抽样基础权重，不是分析后校正权重。"""
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        raise ValueError("权重之和必须为正")
    return sum(v * w for v, w in pairs) / total_w


def sample_sd(values):
    values = list(values)
    if len(values) < 2:
        return float("nan")
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


# --------------------------------------------------------------------------- #
# 分布函数：正则化不完全 Beta（连分式），由此得到 t 与 F 的尾概率
# --------------------------------------------------------------------------- #
def _betacf(a, b, x, max_iter=200, eps=3e-14):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betai(a, b, x):
    """正则化不完全 Beta 函数 I_x(a,b)。"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_sf(t, df):
    """Student t 分布的上尾（双侧 p = 2*sf(|t|)）。"""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    return 0.5 * betai(df / 2.0, 0.5, x)


def f_sf(f, df1, df2):
    """F 分布上尾概率。"""
    if f <= 0:
        return 1.0
    x = df2 / (df2 + df1 * f)
    return betai(df2 / 2.0, df1 / 2.0, x)


@dataclass(frozen=True)
class PairedResult:
    n_pairs: int
    point_difference: float
    sd_difference: float
    ci_low: float
    ci_high: float
    t_stat: float
    p_value: float
    method: str


def paired_estimate(differences, weights, households, reps=DEFAULT_BOOTSTRAP_REPS,
                    seed=DEFAULT_SEED, ci_level=0.95):
    """配对差值的点估计、t 检验与家庭聚类加权自助置信区间。

    differences/weights/households 等长且按同一顺序对齐（每个学生一行）。
    自助重采样单位是家庭：同一家庭的两名学生始终同时进入或离开重采样样本，
    因此不会把非独立观测当作独立证据。

    口径说明：点估计与自助区间使用抽样基础权重（加权估计量）；配对 t 检验是
    等权经典检验，作为方向性参考，与加权估计量并列报告，不声称二者是同一估计量。
    """
    differences = [float(v) for v in differences]
    n = len(differences)
    if n != len(weights) or n != len(households):
        raise ValueError("差值、权重与家庭序列长度不一致")
    if n < 2:
        raise ValueError("配对样本不足，无法估计差值")
    if len(set(households)) < 2:
        raise ValueError("家庭聚类数不足 2，聚类自助区间不可定义")
    point = weighted_mean(list(zip(differences, weights)))
    sd_d = sample_sd(differences)
    m = mean(differences)
    se = sd_d / math.sqrt(n)
    t_stat = (m - 0.0) / se if se > 0 else float("inf")
    p_value = 2.0 * t_sf(abs(t_stat), n - 1) if se > 0 else float("nan")

    groups: dict[str, list[tuple[float, float]]] = {}
    for diff, weight, house in zip(differences, weights, households):
        groups.setdefault(house, []).append((diff, weight))
    cluster_ids = list(groups)
    rng = random.Random(seed)
    boot = []
    for _ in range(reps):
        chosen = [rng.choice(cluster_ids) for _ in cluster_ids]
        num = den = 0.0
        for cid in chosen:
            for diff, weight in groups[cid]:
                num += diff * weight
                den += weight
        boot.append(num / den)
    boot.sort()
    alpha = (1.0 - ci_level) / 2.0
    lo = boot[max(0, int(alpha * reps) - 1)] if alpha * reps >= 1 else boot[0]
    hi = boot[min(reps - 1, int((1 - alpha) * reps))]
    return PairedResult(
        n_pairs=n, point_difference=point, sd_difference=sd_d, ci_low=lo, ci_high=hi,
        t_stat=t_stat, p_value=p_value,
        method=f"加权点估计与家庭聚类加权百分位自助（{reps}次，种子{seed}，{int(ci_level*100)}%CI）；"
               f"配对t检验为等权参考检验，不替代加权区间",
    )


# --------------------------------------------------------------------------- #
# 最小二乘：高斯消元解正规方程，系数 t 检验
# --------------------------------------------------------------------------- #
def ols(y, x_rows):
    """带截距的 OLS。x_rows 为不含截距的解释变量行列表。"""
    n = len(y)
    x = [[1.0, *row] for row in x_rows]
    k = len(x[0])
    if n <= k:
        raise ValueError("观测数不足以拟合该模型")
    xtx = [[sum(x[i][a] * x[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    xty = [sum(x[i][a] * y[i] for i in range(n)) for a in range(k)]
    beta = _solve(xtx, xty)
    fitted = [sum(beta[a] * x[i][a] for a in range(k)) for i in range(n)]
    resid = [y[i] - fitted[i] for i in range(n)]
    dof = n - k
    sigma2 = sum(r * r for r in resid) / dof
    inv = _invert(xtx)
    se = [math.sqrt(max(sigma2 * inv[a][a], 0.0)) for a in range(k)]
    tvals = [beta[a] / se[a] if se[a] > 0 else float("inf") for a in range(k)]
    pvals = [2.0 * t_sf(abs(tvals[a]), dof) if se[a] > 0 else float("nan") for a in range(k)]
    sst = sum((v - mean(y)) ** 2 for v in y)
    r_squared = 1.0 - sigma2 * dof / sst if sst > 0 else float("nan")
    return {"beta": beta, "se": se, "t": tvals, "p": pvals, "dof": dof, "resid": resid,
            "r_squared": r_squared}


def weighted_cluster_mean_ci(values, weights, households, reps=DEFAULT_BOOTSTRAP_REPS,
                             seed=DEFAULT_SEED, ci_level=0.95):
    """单组加权均值的家庭聚类百分位自助区间。"""
    groups: dict[str, list[tuple[float, float]]] = {}
    for v, w, house in zip(values, weights, households):
        groups.setdefault(house, []).append((float(v), float(w)))
    cluster_ids = list(groups)
    if len(cluster_ids) < 2:
        raise ValueError("聚类数量不足，无法自助")
    rng = random.Random(seed)
    boot = []
    for _ in range(reps):
        num = den = 0.0
        for cid in (rng.choice(cluster_ids) for _ in cluster_ids):
            for v, w in groups[cid]:
                num += v * w
                den += w
        boot.append(num / den)
    boot.sort()
    alpha = (1.0 - ci_level) / 2.0
    lo = boot[max(0, int(alpha * reps) - 1)]
    hi = boot[min(reps - 1, int((1 - alpha) * reps))]
    point = weighted_mean(list(zip(values, weights)))
    return point, lo, hi


def f_test_rss(rss_full, rss_reduced, q, dof_full):
    """嵌套模型 F 检验：q 个约束。"""
    if rss_full <= 0:
        return float("nan"), float("nan")
    f = ((rss_reduced - rss_full) / q) / (rss_full / dof_full)
    return f, f_sf(f, q, dof_full)


def _solve(a, b):
    n = len(a)
    mat = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(mat[r][col]))
        if abs(mat[pivot][col]) < 1e-12:
            raise ValueError("设计矩阵奇异")
        mat[col], mat[pivot] = mat[pivot], mat[col]
        pivot_val = mat[col][col]
        mat[col] = [v / pivot_val for v in mat[col]]
        for r in range(n):
            if r == col:
                continue
            factor = mat[r][col]
            mat[r] = [rv - factor * cv for rv, cv in zip(mat[r], mat[col])]
    return [mat[i][n] for i in range(n)]


def _invert(a):
    n = len(a)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("设计矩阵奇异")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_val = aug[col][col]
        aug[col] = [v / pivot_val for v in aug[col]]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            aug[r] = [rv - factor * cv for rv, cv in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]
