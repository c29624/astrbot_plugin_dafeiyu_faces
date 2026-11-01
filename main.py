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
PLUGIN_VERSION = "1.3.0"

DEFAULT_POOLS = [
    # —— 基础包（日常情绪）——
    "害羞软乎=(〃▽〃)|(/ω＼)|(⁄•⁄ω⁄⁄)|(*/ω＼*)|(//▽//)|(๑//ω//๑)|(〃ω〃)|(*ﾉωﾉ)|(//ω//)|(*/▽＼*)|(。>///<。)|(〃>///<〃)|((//>_<//))|(＃>///<)|(⁄ ⁄•⁄-⁄•⁄ ⁄)|(⁄ ⁄>⁄ ⁄<⁄ ⁄)|(⁄ ⁄>⁄◡⁄<⁄ ⁄)|(つω`｡)|(〃ᗜ〃)|(´つヮ⊂)|(,,>᎑<,,)|(⸝⸝>᎑<⸝⸝)|(⸝⸝•ᴗ•⸝⸝)",
    "嘴馋流口水=(¯﹃¯)|(￣﹁￣)|(´～`)|ԅ(¯﹃¯ԅ)|(￣￢￣)|(๑´ڡ`๑)|(๑>ڡ<๑)|(*´﹃｀*)|(ŏ﹃ŏ)|(*^q^*)|(*¯﹃¯*)|( ๑ ´ ﹃ ` ๑ )|( ˶ • ﹃ • ˶ )|( ๑ ᵕ ﹃ ᵕ ๑ )|( ๑ ° ﹃ ° ๑ )|(*´ч`*)|( ¯﹃¯ )ﾉ|(´¬`)|(＾¬＾)|(*•ڡ•*)|( ˘ڡ˘ )ﾉ|( ˃ ³ ˂ )",
    "得意开心=(≧▽≦)|(＾▽＾)|(￣▽￣)|(´▽`)|(●´∀｀●)|o(≧▽≦)o|(￣▽￣)~*|(´∀`)|(⌒▽⌒)|(ﾉ´▽｀)ﾉ♪|(｡◕‿◕｡)|٩(ˊᗜˋ*)و",
    "炸毛生气=(｀Д´)|(╯‵□′)╯︵┻━┻|(￣ヘ￣)|ヽ(#`Д´)ﾉ|(╬￣皿￣)|٩(๑`н´๑)۶|(ノ｀Д´)ノ|(＞д＜)",
    "委屈想哭=(´；ω；`)|(╥﹏╥)|(;´Д`)|(T﹏T)|(ಥ_ಥ)|(┬＿┬)|(｡•́︿•̀｡)|(´•︵•`)|(´･ω･`)|┭┮﹏┭┮|(T_T)|(╥_╥)|(´;ω;`)|(ノ_<、)|(இ﹏இ)|( ;∀;)|(つД`)|(இдஇ)|｡ﾟ(ﾟ´Д`ﾟ)ﾟ｡|(。>﹏<。)|o(╥﹏╥)o|(づ╥﹏╥)づ|(っ˘ω˘ς ;)",
    "犯困迷糊=(´-ω-)|(˘ω˘)|(´ｚ`)|(´-ωก̀)|(。´-ω-)|(ｕ_ｕ)|(￣o￣)|( ˘ω˘ )zzz|(｡-ω-)zzZ|(ᴗ˳ᴗ)|(-ω-)|(_ _).｡o○|( ˘ω˘ )ﾉﾞ|(∪｡∪)|(˘ᴗ˘)|( ´ρ`)|(*´ρ`*)|(｡-ω-)zzzゞ|(-ω-｡)|( ´ -｀)zzZ|(˘･ω･˘ ).oO|(つ﹏<)z",
    "惊讶吓到=(⊙ˍ⊙)|(⊙o⊙)|(゜ロ゜)|w(ﾟДﾟ)w|(°□°；)|Σ(ﾟДﾟ；)|(⊙_⊙)|(☉_☉)|( ﾟдﾟ)|(⊙▽⊙)|(@口@)",
    "无语汗颜=(￣_￣)|(¬_¬)|(￢_￢)|(눈_눈)|(＝_＝)|(\"▔□▔)|(￣ー￣;)",
    "无奈摊手=╮(╯▽╰)╭|╮(￣▽￣)╭|┐(─__─)┌|╮(╯_╰)╭|╮(﹀_﹀)╭|(´-ω-｀)",
    "满足享受=(￣ˇ￣)|(￣︶￣)|(｡￫‿￩｡)|(=ˇωˇ=)|(˘︶˘)|<(￣︶￣)>|(´,,•ω•,,)|(˶ᵔ ᵕ ᵔ˶)|o(*￣︶￣*)o|(*´ω`*)",
    "傲娇哼=o(｀ω´*)o|(￣^￣)|(￣へ￣)|(｀へ´)|(ˉ▽￣～)|(｀_´)ゞ|(￣ε(#￣)|( ˇωˇ ) フフン",
    "猫爪蹭蹭=ฅ´ω`ฅ|(ฅ´ω`ฅ)|ฅ(＾ω＾)ฅ|ʕ•̀ω•́ʔ✧|(=ΦωΦ=)|(=^･ω･^=)|(=✧ω✧=)|(=ↀωↀ=)|(=｀ω´=)",
    "加油打气=(ง •̀_•́)ง|(*•̀ㅂ•́)و|(๑•̀ㅂ•́)و✧|(๑•̀ㅕ•́)و✧|(ง•̀ω•́)ง|(ง˙ω˙)ง",
    "打招呼挥手=(´▽`)ﾉ|(´ω`)ノ|(・ω・)ノ|ヾ(´∀｀)ﾉ|(￣▽￣)ノ|(｡･∀･)ﾉﾞ|(*´ω`*)ﾉ|( ´∀`)ﾉ|( =ω=)ﾉ|( ◕ᴗ◕)ﾉ|(=⌒‿‿⌒=)|( ´ ▽ ` )ﾉ|(・∀・)ﾉ|(*>ω<)ﾉ|(TェT)ﾉ|(=ΦωΦ=)ﾉ",
    "星星眼期待=(☆▽☆)|(☆ω☆)|(★‿★)|(✧ω✧)|(✧◡✧)|( ✧∀✧)|(★ω★)|(✪ω✪)|(✪▽✪)|(✰‿✰)|(★ｗ★)|(★ε★)|(☆Д☆)|(✰ω✰)|(☆▿☆)",
    # —— 大肥鱼本鱼专属（kaomojis.jp 鱼页全站精选，纯文本无 emoji）——
    "鱼鱼游动=>°)))彡|><(((º>|<゜)))彡|>°)))<><|ミ(°°)ミ|>(•ω•)<彡|＞（口）＜彡|くコ:彡|くコ:彡（*´∀`）|～～>゜)))彡|ζ°)))彡|>゜))))彡|ﾐ(ˊᗜˋ)ﾐ|ﾐ(∩ω∩)ﾐ|∿∿（*´ω`*）∿∿|（´◡`）ノ彡～|ヽ(≧∀≦)ノ彡|≧(´▽｀)≦彡",
    # —— 亲密包（暧昧/RP 过渡带）——
    "比心亲亲=(づ￣ 3￣)づ|(˘ε˘)|(´ε｀ʃƪ)|( ˘³˘)♥|(っ´ω`c)|٩(♡ε♡)۶|(๑ˇεˇ๑)|(⊃｡•́‿•̀｡)⊃|(っ˘ω˘ς)|(♡´▽`♡)|(❤ω❤)|(๑♡⌓♡๑)|(* ￣3)(ε￣ *)|(っ´Ι`)っ|～(^з^)-☆|(◍•ᴗ•◍)❤|(¯³¯)♡|( ・ε・)❤|ε(*´・ω・)з|(◕‿◕)ε³~♡|( ´ з`)ノ⌒♡|(*´ 3`)/~♡|(ノ´ з`)ノ♡|(˶ᵔᴗᵔ)ε♡|( ˶˘ ³˘˵)|(っ＞◡＜)っ|♡(.◜ω◝.)♡|✿(˘ε˘✿)♡",
    "暧昧脸红=(¬‿¬)|(⁄ ⁄•⁄ω⁄•⁄ ⁄)|(⁄ ⁄>⁄ ▽ ⁄<⁄ ⁄)|(⁄˘⁄ ω ˘⁄ ♡)|(⁄ ›ω‹ ⁄)|(///∇///)|(///▽///)|(´////`)|(¦3[▓▓]|ο(=•ω＜=)ρ⌒☆|( ˶ˆᗜˆ˵ )|(⸝⸝⸝•﹏•⸝⸝⸝)|(♡´艸`)|( ⁄ʘ⁄ ⁄ ⁄ʘ⁄)",
    "失神迷离=(×⌓×)|(＞﹏＜)|(×﹏×)|(♡о♡)|(♡˙︶˙♡)|(*♡∀♡)|(♡⌄♡)|( ♥д♥)|(♥ε♥)|(♥ﾛ♥*)|(♡▽♡)|(˶♡‿♡˶)|(♡‿♡)",
    # —— 18+ 包（成年向软款，默认关；不做 2ch 硬核款——QQ 里出戏）——
    "沉溺发软=(｡♥‿♥｡)|(♥‿♥)|(♡ω♡ )|(灬ºωº灬)|(⁄▔﹃▔⁄)|(⁄〇⁄ω⁄〇⁄)|(灬♥ω♥灬)|(♥ω♥*)|｡ﾟ♡ﾟ｡(｡>‿‿<｡ )｡ﾟ♡ﾟ｡",
    # —— 雌小鬼包（嚣张欠揍系，默认关）——
    "嚣张欠揍=( ˘ω˘ ) ｽﾔｧ|(ΦωΦ)|(∪∀∪)|(・ω<)☆|(・∀・)|(・`ω´・)|(σ｀д′)σ|(¬､¬)|(￣ー￣)ﾆﾔﾘ☆|( ˘⌣˘)♪☆|(¬ω¬)ﾉ♪|(￣∀￣)ゞ☆|ԅ(≖‿≖ԅ)|(≖ᴗ≖✿)|ψ(｀∇´)ψ♪|(˶˃ ᗜ ˂˶)ゞ✧|(⌐‿⌐)|ᕕ(⌐■_■)ᕗ|( ˇ⊖ˇ)♪♫|╮(ˆ⌣ˆ)╭",
]

# 池名 → 所属包：faces_packs 勾了哪个包，哪包的池才装载；
# 自己在 faces_pools 里加的自定义池不在表里 = 恒装载，不受包开关影响
POOL_PACKS = {
    "害羞软乎": "基础", "嘴馋流口水": "基础", "得意开心": "基础", "炸毛生气": "基础",
    "委屈想哭": "基础", "犯困迷糊": "基础", "惊讶吓到": "基础", "无语汗颜": "基础",
    "无奈摊手": "基础", "满足享受": "基础", "傲娇哼": "基础", "猫爪蹭蹭": "基础",
    "加油打气": "基础", "打招呼挥手": "基础", "星星眼期待": "基础", "鱼鱼游动": "基础",
    "比心亲亲": "亲密", "暧昧脸红": "亲密", "失神迷离": "亲密",
    "沉溺发软": "18+",
    "嚣张欠揍": "雌小鬼",
}
ALL_PACKS = ["基础", "亲密", "18+", "雌小鬼"]
DEFAULT_PACKS = ["基础", "亲密"]

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
        self.c_pool_cfg = [
            str(s).strip()
            for s in (cfg.get("faces_pools") or DEFAULT_POOLS)
            if str(s).strip() and "=" in str(s)
        ] or list(DEFAULT_POOLS)
        # 包开关：faces_packs 勾选启用哪些包（基础/亲密/18+/雌小鬼）；
        # 未勾的包整体不装载。自定义池（不在 POOL_PACKS）恒装载
        self.c_packs = [
            str(x).strip() for x in (cfg.get("faces_packs") or DEFAULT_PACKS) if str(x).strip()
        ] or list(DEFAULT_PACKS)
        pack_set = set(self.c_packs)
        # 池解析：脸 → 同池脸列表；脸 → 池名；按"长脸优先"匹配防子串误中
        self._pool_of: dict = {}
        self._pool_name: dict = {}
        self._pool_entries: list = []  # (池名, 张数, 是否装载) 供状态指令展示
        skipped = []
        for entry in self.c_pool_cfg:
            name, _, faces_str = entry.partition("=")
            pname = name.strip()
            faces = [f.strip() for f in faces_str.split("|") if f.strip()]
            pack = POOL_PACKS.get(pname)
            if pack and pack not in pack_set:
                skipped.append(f"{pname}({pack})")
                self._pool_entries.append((pname, len(faces), False))
                continue
            self._pool_entries.append((pname, len(faces), True))
            for f in faces:
                self._pool_of.setdefault(f, [x for x in faces])
                self._pool_name.setdefault(f, pname)
        self._faces_sorted = sorted(self._pool_of.keys(), key=len, reverse=True)
        if skipped:
            logger.info(
                f"[随机颜文字] 启用包：{'、'.join(self.c_packs)}；跳过未开包的池：{'、'.join(skipped)}（faces_packs 里加包名可开启）"
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
                    # 或单脸超限（v1.3.0 热度计数≥hot_max，治隔条复读的百搭脸）
                    is_hot = self.c_hot_max > 0 and hot_cnt.get(face, 0) >= self.c_hot_max
                    is_swap = window == 0 or (window > 0 and face in recent_faces) or is_hot
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
        yield event.plain_result(
            f"[随机颜文字] 开关：{'开' if self.c_on else '关'}｜启用包：{'、'.join(self.c_packs)}｜连用窗口：{self.c_window} 条｜累计换脸：{self._swap_count} 次\n"
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
