"""随机颜文字（astrbot_plugin_dafeiyu_faces）

被动换脸：模型自己管"什么时候配脸、配什么情绪"（它擅长），
本插件管"别老用同一张脸"（它不擅长——示例列表里排第一的百搭脸会被
锚定后连用几十次，实测一场对话 42 条清一色 (´･ω･`)）。

机制：LLM 回复里的颜文字若在该会话最近 N 条消息里出现过，
就从**同情绪池**里随机换一张——时机和情绪都是模型选的，库只管多样性。

边界（fail-open）：
- 只认库内精确匹配的脸，防止泛匹配误伤（）动作描写；不认识的脸一律放行
- 换不出（池里没有别的可选）就保留原脸
- 任何异常都放行原消息，绝不因换脸吞消息
- 只处理模型生成文本（is_model_result），指令/工具消息不动

覆盖不到的路径（直发不经管线，v1 不处理）：主动私聊（dafeiyu_dm）、
睡觉插件的梦话/告别语。
"""

import json
import random
from collections import deque
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_dafeiyu_faces"
PLUGIN_VERSION = "2.2.0"

# v1.4.0 上游注入：把选脸/拟声词规则注进 system_prompt（教上游），跟人设解耦——
# 人设示例列表排第一的百搭脸会被锚定连用；改池改文案不再要动人设。
# {pools} 占位符运行时填"当前装载的池名列表"；文案清空=不注入。
DEFAULT_PROMPT_HINT = (
    "颜文字按当下情绪从这些类别里换着挑一张（{pools}）——同一个别连着用、别固定一张；"
    "拟声词也换着用（唔、嗯、呀、哈…按当下感觉来），放开头或单独成小句，别嵌进句子中间"
)
_INJECT_TAG = "[颜文字·拟声词风格]"

# v2.1.0 词典内置：51 分类成品脸随插件分发（kaomojis.jp 官方 IME 词典机械生成），
# 代码只管装载与勾选；faces_pools 降级为"自定义附加池"（覆盖同名分类或新增池）
_DICT_PATH = Path(__file__).parent / "kaomoji_dict.json"


def _load_builtin_pools() -> dict:
    """读内置词典：分类名 → [脸...]。文件缺失/损坏返回空 dict（fail-open：只剩自定义池）"""
    try:
        data = json.loads(_DICT_PATH.read_text(encoding="utf-8"))
        cats = data.get("categories") or {}
        return {str(k): [str(f) for f in v if str(f)] for k, v in cats.items()}
    except Exception:
        logger.error("[随机颜文字] 内置词典 kaomoji_dict.json 读取失败", exc_info=True)
        return {}


BUILTIN_POOLS = _load_builtin_pools()
ALL_CATEGORIES = list(BUILTIN_POOLS.keys())

# 池名 → 所属包：faces_packs 勾了哪个包，哪包的池才装载；
# 自己在 faces_pools 里加的自定义池不在表里 = 恒装载，不受包开关影响


RECENT_UMO_CAP = 50  # 记多少个会话的最近用脸（防状态文件无限长）


@register(
    PLUGIN_NAME,
    "c29624",
    "随机颜文字：同款颜文字连用就换——模型管时机情绪，库管多样性",
    PLUGIN_VERSION,
)
class DafeiyuFacesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        cfg = config or {}
        self.c_on = bool(cfg.get("faces_enabled", True))
        try:
            self.c_window = max(0, int(cfg.get("faces_repeat_window", 2)))
        except Exception:
            self.c_window = 2
        # 单脸上限（v1.3.0）：治"百搭脸隔条复读"——同款连用窗口拦得住紧挨着的重复，
        # 拦不住隔一条用一次的刷屏脸；最近 hot_window 条里同一张脸≥hot_max 次就强制换
        try:
            self.c_hot_max = max(0, int(cfg.get("faces_hot_max", 3)))
            self.c_hot_window = max(0, int(cfg.get("faces_hot_window", 20)))
        except Exception:
            self.c_hot_max, self.c_hot_window = 3, 20
        # v1.4.0 上游注入 + 重点脸黑名单
        self.c_prompt_on = bool(cfg.get("faces_prompt_on", True))
        _hint = cfg.get("faces_prompt_hint")
        self.c_prompt_hint = (
            DEFAULT_PROMPT_HINT if _hint is None or not str(_hint).strip() else str(_hint).strip()
        )
        self._blacklist = {
            str(x).strip() for x in (cfg.get("faces_blacklist") or []) if str(x).strip()
        }
        # v2.1.0 分类勾选：faces_categories 勾哪个内置分类装哪个；没配/空 = 全部内置分类
        cats_cfg = [
            str(x).strip() for x in (cfg.get("faces_categories") or []) if str(x).strip()
        ]
        enabled = set(cats_cfg) if cats_cfg else set(BUILTIN_POOLS)
        # 自定义附加池（v2.1.0 起 faces_pools 只做附加：覆盖同名内置分类或新增池；
        # 旧版配置里等于内置默认的行覆盖进来内容相同，行为不变）
        custom = [
            str(s).strip()
            for s in (cfg.get("faces_pools") or [])
            if str(s).strip() and "=" in str(s)
        ]
        # 池解析：脸 → 同池脸列表；脸 → 池名；按"长脸优先"匹配防子串误中
        self._pool_of: dict = {}
        self._pool_name: dict = {}
        self._pool_entries: list = []  # (池名, 张数, 是否装载) 供状态指令展示
        skipped = [n for n in BUILTIN_POOLS if n not in enabled]
        for pname, faces in BUILTIN_POOLS.items():
            if pname not in enabled:
                self._pool_entries.append((pname, len(faces), False))
                continue
            self._pool_entries.append((pname, len(faces), True))
            for f in faces:
                self._pool_of.setdefault(f, list(faces))
                self._pool_name.setdefault(f, pname)
        for entry in custom:
            name, _, faces_str = entry.partition("=")
            pname = name.strip()
            # v2.2.0 双轨分隔：行内见全角｜就按｜切（半角|原样留在脸里，适配 |ω・) 这类）；
            # 没有｜才按旧半角|切（老配置不动照跑）
            sep = "｜" if "｜" in faces_str else "|"
            faces = [f.strip() for f in faces_str.split(sep) if f.strip()]
            if pname in self._pool_name.values():
                # 覆盖同名池：先把旧池的脸撤下来再挂新脸
                old_faces = [f for f, n in self._pool_name.items() if n == pname]
                for f in old_faces:
                    self._pool_of.pop(f, None)
                    self._pool_name.pop(f, None)
                self._pool_entries = [(e if e[0] != pname else (pname, len(faces), True))
                                       for e in self._pool_entries]
            else:
                self._pool_entries.append((pname, len(faces), True))
            for f in faces:
                self._pool_of.setdefault(f, list(faces))
                self._pool_name.setdefault(f, pname)
        self._faces_sorted = sorted(self._pool_of.keys(), key=len, reverse=True)
        if skipped:
            logger.info(
                f"[随机颜文字] 装载分类：{len(BUILTIN_POOLS) - len(skipped)}/{len(BUILTIN_POOLS)}；"
                f"未勾选跳过：{'、'.join(skipped)}（faces_categories 里勾分类名可开启）"
            )
        # 最近用脸：umo → deque(每条消息用过的脸列表)
        self._recent: dict = {}
        self._swap_count = 0
        self._data_dir = None
        self._load_state()

    # ---------- 状态持久化（BOM-less utf-8） ----------

    def _state_path(self) -> Path:
        if self._data_dir:
            return Path(self._data_dir) / "faces_state.json"
        return Path("data/plugin_data") / PLUGIN_NAME / "faces_state.json"

    def _load_state(self):
        try:
            p = self._state_path()
            if p.exists():
                v = json.loads(p.read_text(encoding="utf-8"))
                self._recent = {
                    k: deque(lst, maxlen=max(1, self.c_window or 3, self.c_hot_window or 0))
                    for k, lst in (v.get("recent") or {}).items()
                }
                self._swap_count = int(v.get("swap_count") or 0)
        except Exception:
            logger.warning("[随机颜文字] 状态读取失败，从空状态开始", exc_info=True)
            self._recent = {}

    def _save_state(self):
        try:
            p = self._state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            if len(self._recent) > RECENT_UMO_CAP:
                for k in list(self._recent.keys())[: len(self._recent) - RECENT_UMO_CAP]:
                    self._recent.pop(k)
            data = {
                "recent": {k: list(dq) for k, dq in self._recent.items()},
                "swap_count": self._swap_count,
            }
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.warning("[随机颜文字] 状态保存失败（不影响换脸）", exc_info=True)

    async def initialize(self):
        try:
            from astrbot.api.star import StarTools

            self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            self._data_dir = Path("data/plugin_data") / PLUGIN_NAME
        self._load_state()  # 拿到正式数据目录后重载一次

    async def terminate(self):
        self._save_state()

    # ---------- 换脸主逻辑 ----------

    def _style_hint(self) -> str:
        """渲染注入文案：{pools} 填当前装载的池名；空文案=不注入"""
        txt = (self.c_prompt_hint or "").strip()
        if not txt:
            return ""
        if "{pools}" in txt:
            loaded = "、".join(name for name, _, on in self._pool_entries if on)
            txt = txt.replace("{pools}", loaded or "（无装载池）")
        return _INJECT_TAG + "\n" + txt

    @filter.on_llm_request(priority=50)
    async def inject_style_hint(self, event: AstrMessageEvent, req=None, *args, **kwargs):
        """把选脸/拟声词规则追加进 system_prompt（教上游，v1.4.0）。
        防重：system_prompt 里已有本注入块就先整体剥掉再追加（流式重试/钩子重入不叠加）。
        任何异常放行（fail-open）。"""
        if not self.c_prompt_on:
            return
        try:
            hint = self._style_hint()
            if not hint:
                return
            base = str(getattr(req, "system_prompt", "") or "")
            if _INJECT_TAG in base:
                base = base.split(_INJECT_TAG, 1)[0].rstrip("\n")
            req.system_prompt = (base + "\n\n" if base else "") + hint
        except Exception:
            logger.warning("[随机颜文字] 风格注入失败（放行）", exc_info=True)

    @filter.on_decorating_result(priority=100)
    async def swap_repeated_faces(self, event: AstrMessageEvent, *args, **kwargs):
        """同款连用就换：priority=100 保证在分段发送（默认0）等
        后续 decorate 钩子之前完成换脸。任何异常放行（fail-open）。"""
        if not self.c_on:
            return
        try:
            result = event.get_result()
            if not result or not getattr(result, "chain", None):
                return
            if not result.is_model_result():
                return
            umo = getattr(event, "unified_msg_origin", "") or "?"
            plains = [c for c in result.chain if isinstance(c, Plain)]
            if not plains:
                return

            window = self.c_window
            recent_faces = set()
            hot_cnt: dict = {}
            history = self._recent.get(umo)
            msgs = list(history) if history else []
            if window > 0:
                for lst in msgs[-window:]:
                    recent_faces.update(lst)
            if self.c_hot_max > 0:
                hot_cnt = {}
                for lst in msgs[-(self.c_hot_window or 20):]:
                    for f in lst:
                        hot_cnt[f] = hot_cnt.get(f, 0) + 1

            used_now: list = []  # 本条消息最终呈现的脸（换后的），记账用
            changed = False
            for p in plains:
                text = getattr(p, "text", "") or ""
                if not text:
                    continue
                # 长脸可能藏着短脸（(;´Д`)ﾊｧﾊｧ 里嵌着 (;´Д`)、(´▽`)ﾉ 里嵌着 (´▽`)）：
                # 只处理"不是别的已匹配脸的子串"的那些，防止换短脸时把长脸劈坏
                matched = [f for f in self._faces_sorted if f in text]
                top = [
                    f
                    for f in matched
                    if not any(f != g and f in g for g in matched)
                ]
                for face in top:
                    if face not in text:
                        continue
                    pool = self._pool_of.get(face) or []
                    candidates = [
                        f
                        for f in pool
                        if f != face and f not in recent_faces and f not in used_now
                    ]
                    if not candidates:
                        candidates = [f for f in pool if f != face]
                    # 换脸条件：同款连用（window>0）；window=0 = 每张都随机发牌；
                    # 或单脸超限（v1.3.0 热度计数≥hot_max，治隔条复读的百搭脸）；
                    # 或点名黑名单（v1.4.0 faces_blacklist，列了就每次必换）
                    is_hot = self.c_hot_max > 0 and hot_cnt.get(face, 0) >= self.c_hot_max
                    is_swap = (
                        window == 0
                        or (window > 0 and face in recent_faces)
                        or is_hot
                        or face in self._blacklist
                    )
                    if not is_swap and candidates:
                        used_now.append(face)  # 没重复：尊重模型自己的选择
                        continue
                    if not candidates:
                        used_now.append(face)  # 池里没得换：放行
                        continue
                    new_face = random.choice(candidates)
                    text = text.replace(face, new_face)
                    changed = True
                    self._swap_count += 1
                    used_now.append(new_face)
                    logger.info(
                        f"[随机颜文字] {face} → {new_face}（{self._pool_name.get(face, '?')}）"
                    )
                p.text = text

            # 记账：本条消息实际呈现的脸进 recent（记换后的，用户真看到的）
            dq = self._recent.setdefault(
                umo, deque(maxlen=max(1, window or 3, self.c_hot_window or 0))
            )
            dq.append(list(dict.fromkeys(used_now)))
            if changed:
                self._save_state()
        except Exception:
            logger.warning("[随机颜文字] 换脸钩子异常，本条放行", exc_info=True)

    # ---------- 调试指令 ----------

    @filter.command("颜文字状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看换脸插件状态（仅管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("没权限")
            return
        pools = []
        for pname, n, active in self._pool_entries:
            mark = "" if active else "（包未开）"
            pools.append(f"· {pname}：{n} 张{mark}")
        recent_bits = [
            f"{k.split(':')[-1]}: {'、'.join(f for lst in v for f in lst) or '无'}"
            for k, v in list(self._recent.items())[-5:]
        ]
        bl = "、".join(self._blacklist) or "无"
        _loaded = sum(1 for e in self._pool_entries if e[2])
        en, tot = _loaded, len(self._pool_entries)
        cst = sum(1 for e in self._pool_entries if e[0] not in BUILTIN_POOLS)
        yield event.plain_result(
            f"[随机颜文字] 开关：{'开' if self.c_on else '关'}｜分类：{en}/{tot}｜自定义池：{cst}｜连用窗口：{self.c_window} 条｜累计换脸：{self._swap_count} 次\n"
            f"风格注入：{'开' if self.c_prompt_on else '关'}｜必换黑名单：{bl}\n"
            "情绪池：\n" + "\n".join(pools)
            + ("\n最近用脸：\n" + "\n".join(recent_bits) if recent_bits else "")
        )

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False
