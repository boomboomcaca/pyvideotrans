"""CosyVoice2/3 webui (port 8000) client.

Supports four inference modes auto-dispatched per item:

- 预训练音色   (sft):           role == one of cosyvoice_sft_roles
- 自然语言控制 (instruct2):     clone role + instruct text (global or per-line)
- 3s极速复刻   (zero_shot):     clone role + reference text (highest quality)
- 跨语种复刻   (cross_lingual): clone role without reference text

Per-line instruct: role string may carry an instruct suffix as ``role|instruct text``.
"""
import random
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Union

from gradio_client import handle_file

from videotrans.configure.config import params, logger, tr
from videotrans.configure.excepts import StopRetry
from videotrans.tts._gradio import GradioBase
from videotrans.util import tools

MODE_SFT = '预训练音色'
MODE_INSTRUCT = '自然语言控制'
MODE_CROSS_LINGUAL = '跨语种复刻'
MODE_ZERO_SHOT = '3s极速复刻'

# 进程内缓存：每个 webui URL 的真实可用 SFT 列表
_sft_cache: Dict[str, List[str]] = {}
_sft_cache_lock = threading.Lock()


@dataclass
class CosyVoice(GradioBase):
    """CosyVoice2/3 gradio webui client with full 4-mode dispatch."""

    def __post_init__(self):
        self.ainame = "cosyvoice"
        super().__post_init__()
        # CosyVoice 服务端 speed 取值范围 0.5..2.0
        self.speed = max(0.5, min(2.0, self.get_speed()))
        # 覆盖 GradioBase 默认 roledict（仅 f5tts 克隆音色）为合并后的 CosyVoice 角色表
        self.roledict = tools.get_cosyvoice_rolelist()

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_role_and_instruct(role_raw: str) -> Tuple[str, str]:
        """支持 ``role|instruct text`` 的按行 instruct 写法。"""
        if not role_raw:
            return '', ''
        if '|' in role_raw:
            r, instr = role_raw.split('|', 1)
            return r.strip(), instr.strip()
        return role_raw.strip(), ''

    def _is_sft_role(self, role: str) -> bool:
        if not role or role in ('clone', 'No'):
            return False
        if role.lower().endswith('.wav'):
            return False
        info = self.roledict.get(role)
        if isinstance(info, dict) and info.get('sft'):
            return True
        # 兜底：未注册但用户在配置项里写过
        configured = (params.get('cosyvoice_sft_roles', '') or '').replace('，', ',').split(',')
        return role in {c.strip() for c in configured if c.strip()}

    def _resolve_seed(self) -> int:
        try:
            seed = int(params.get('cosyvoice_seed', 0) or 0)
        except (TypeError, ValueError):
            seed = 0
        if seed <= 0:
            seed = random.randint(1, 100000000)
        return seed

    def _detect_webui_sft_choices(self) -> List[str]:
        """探测 webui 上 sft_dropdown 真实可用的内置音色列表，失败返回 []。"""
        with _sft_cache_lock:
            if self.api_url in _sft_cache:
                return _sft_cache[self.api_url]
        try:
            client = self.get_thread_client()
            info = client.view_api(return_format='dict') or {}
            named = info.get('named_endpoints') or {}
            ep = named.get('/generate_audio') or named.get('generate_audio') or {}
            for param in ep.get('parameters') or []:
                if param.get('parameter_name') != 'sft_dropdown':
                    continue
                comp = param.get('component_info') or {}
                if isinstance(comp, dict) and 'choices' in comp:
                    out = [c for c in comp['choices'] if isinstance(c, str) and c]
                    with _sft_cache_lock:
                        _sft_cache[self.api_url] = out
                    return out
                py = param.get('python_type') or {}
                desc = py.get('description', '') if isinstance(py, dict) else ''
                if isinstance(desc, str) and 'Literal[' in desc:
                    out = [c for c in re.findall(r"'([^']+)'", desc) if c]
                    with _sft_cache_lock:
                        _sft_cache[self.api_url] = out
                    return out
            with _sft_cache_lock:
                _sft_cache[self.api_url] = []
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'CosyVoice 探测 SFT 列表失败：{exc}')
            return []

    def _resolve_clone_ref(self, role: str, data_item: dict) -> Tuple[str, str]:
        """复用 BaseTTS.get_ref_wav，但把 role 替换为解析后的纯角色名。"""
        item = dict(data_item)
        item['role'] = role if role else 'clone'
        try:
            return self.get_ref_wav(item)
        except RuntimeError as exc:
            raise StopRetry(str(exc)) from exc

    # ------------------------------------------------------------------
    # 推理入口
    # ------------------------------------------------------------------
    def _run(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, None]:
        if self._exit() or not data_item.get('text', '').strip() or tools.vail_file(data_item.get('filename')):
            return

        role_raw = data_item.get('role') or ''
        role, line_instruct = self._parse_role_and_instruct(role_raw)
        global_instruct = (params.get('cosyvoice_instruct_text', '') or '').strip()
        effective_instruct = (line_instruct or global_instruct).strip()

        is_sft = self._is_sft_role(role)
        ref_wav: Union[str, None] = None
        ref_text: str = ''

        if is_sft:
            mode = MODE_SFT
            # 仅在配置了真实角色名时探测 webui，避免对其它路径产生影响
            choices = self._detect_webui_sft_choices()
            if choices and role not in choices:
                raise StopRetry(tr(
                    'The SFT voice "{role}" is not in the webui choices: {choices}. '
                    'Please pick one of the supported voices or use a clone role.'
                ).format(role=role, choices=choices))
            if choices == [] and ':9233' in self.api_url:
                raise StopRetry(tr(
                    'The legacy cosyvoice-api endpoint (:9233) does not support built-in SFT voices. '
                    'Please switch to the official Gradio webui (without :9233) or pick a clone role.'
                ))
        else:
            ref_wav, ref_text = self._resolve_clone_ref(role or 'clone', data_item)
            if effective_instruct:
                mode = MODE_INSTRUCT
            elif ref_text:
                mode = MODE_ZERO_SHOT
            else:
                mode = MODE_CROSS_LINGUAL

        seed = self._resolve_seed()

        kwargs = {
            "tts_text": data_item.get('text', '').strip(),
            "mode_checkbox_group": mode,
            "sft_dropdown": role if is_sft else '',
            "prompt_text": ref_text or '',
            "prompt_wav_upload": handle_file(ref_wav) if ref_wav else None,
            "prompt_wav_record": None,
            "instruct_text": effective_instruct,
            "seed": seed,
            "stream": False,
            "speed": self.speed,
            "api_name": "/generate_audio",
        }

        logger.debug(
            f"[CosyVoice] mode={mode} role={role!r} sft={is_sft} ref_wav={Path(ref_wav).name if ref_wav else None} "
            f"instruct={effective_instruct!r} seed={seed}"
        )

        # GradioBase._send 把 ValueError/TypeError 等捕获后返回字符串错误，
        # 这里把跟 SFT 相关的 webui 拒绝信息翻译为更友好的提示。
        result = self._send(kwargs, data_item)
        if isinstance(result, str) and is_sft and 'list of choices' in result:
            return tr(
                'CosyVoice webui rejected SFT voice "{}". The server reports no built-in SFT. '
                'Use a clone role or upgrade to a CosyVoice2-SFT model.'
            ).format(role)
        return result
