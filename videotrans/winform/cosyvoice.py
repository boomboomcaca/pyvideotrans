def openwin():
    import time
    from pathlib import Path

    from PySide6 import QtWidgets

    from videotrans.configure.config import (ROOT_DIR, TEMP_DIR, app_cfg,
                                             logger, params, tr)
    from videotrans.util import tools
    from videotrans.util.ListenVoice import ListenVoice

    def feed(d):
        if d == "ok":
            QtWidgets.QMessageBox.information(winobj, "ok", "Test Ok")
        else:
            tools.show_error(d)
        winobj.test.setText(tr('Test'))

    def _read_form():
        """读取并清洗当前表单为结构化字典。"""
        url = winobj.api_url.text().strip()
        if url and not url.startswith('http'):
            url = 'http://' + url

        sft_text = winobj.sft_roles.toPlainText().strip().replace('，', ',')
        seen = []
        for name in sft_text.split(','):
            name = name.strip()
            if name and name not in seen:
                seen.append(name)
        sft_norm = ','.join(seen)

        seed_text = winobj.seed.text().strip()
        try:
            seed_val = int(seed_text) if seed_text else 0
        except ValueError:
            seed_val = 0
        if seed_val < 0:
            seed_val = 0

        return {
            'cosyvoice_url': url,
            'cosyvoice_instruct_text': winobj.instruct_text.text().strip(),
            'cosyvoice_sft_roles': sft_norm,
            'cosyvoice_seed': seed_val,
        }

    def _probe_webui_sft(url):
        """轻量探测 webui /generate_audio 上 sft_dropdown 的可选列表，失败返回 None。"""
        if not url:
            return None
        try:
            from gradio_client import Client
            client = Client(url, ssl_verify=False, verbose=False)
            info = client.view_api(return_format='dict') or {}
            named = info.get('named_endpoints') or {}
            ep = named.get('/generate_audio') or named.get('generate_audio') or {}
            for param in ep.get('parameters', []) or []:
                if param.get('parameter_name') != 'sft_dropdown':
                    continue
                comp = param.get('component_info') or {}
                if isinstance(comp, dict) and 'choices' in comp:
                    return [c for c in comp['choices'] if isinstance(c, str) and c]
                py = param.get('python_type', {}) or {}
                desc = py.get('description', '') if isinstance(py, dict) else ''
                if isinstance(desc, str) and 'Literal[' in desc:
                    import re as _re
                    return [c for c in _re.findall(r"'([^']+)'", desc) if c]
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'CosyVoice webui 探测失败：{exc}')
            return None

    def test():
        cfg = _read_form()
        if not cfg['cosyvoice_url']:
            return tools.show_error(tr('Please fill in the webui URL first'))

        # 写回 params，TTS 端会读取
        for key, value in cfg.items():
            params[key] = value
        params.save()

        # 优先用 webui 真实支持的 SFT 音色测试
        supported = _probe_webui_sft(cfg['cosyvoice_url'])
        configured = [s.strip() for s in cfg['cosyvoice_sft_roles'].split(',') if s.strip()]

        test_role = None
        if supported:
            chosen = next((c for c in configured if c in supported), supported[0])
            test_role = chosen
            logger.debug(f'[CosyVoice test] using SFT role {chosen} from webui supported list')

        if test_role is None:
            # 回退到最后一个克隆参考音频
            clone_map = tools.get_f5tts_role() or {}
            clone_candidates = [
                (name, info) for name, info in clone_map.items()
                if isinstance(info, dict) and (info.get('ref_wav') or info.get('ref_audio'))
            ]
            if not clone_candidates:
                return tools.show_error(tr(
                    'No SFT voice available and no clone reference audio configured. '
                    'Please set a reference audio or fill in SFT voices.'
                ))
            name, info = clone_candidates[-1]
            ref_filename = info.get('ref_wav') or info.get('ref_audio', '')
            ref_file = Path(ROOT_DIR) / 'f5-tts' / ref_filename
            if not ref_file.exists():
                return tools.show_error(tr('No reference audio {} exists', str(ref_file)))
            test_role = name
            logger.debug(f'[CosyVoice test] falling back to clone role {name}')

        winobj.test.setText(tr('Testing...'))
        from videotrans import tts
        wk = ListenVoice(
            parent=winobj,
            queue_tts=[{
                "text": '你好啊我的朋友,希望你的每一天都美好愉快',
                "role": test_role,
                "filename": TEMP_DIR + f"/{time.time()}-cosyvoice.wav",
                "tts_type": tts.COSYVOICE_TTS,
            }],
            language="zh",
            tts_type=tts.COSYVOICE_TTS,
        )
        wk.uito.connect(feed)
        wk.start()

    def save():
        cfg = _read_form()
        for key, value in cfg.items():
            params[key] = value
        params.save()
        tools.set_process(text='', type="refreshtts")
        winobj.close()

    from videotrans.component.set_form import CosyVoiceForm
    winobj = CosyVoiceForm()
    app_cfg.child_forms['cosyvoice'] = winobj

    # 回填已有配置
    if params.get('cosyvoice_url'):
        winobj.api_url.setText(params.get('cosyvoice_url', ''))
    if params.get('cosyvoice_instruct_text'):
        winobj.instruct_text.setText(params.get('cosyvoice_instruct_text', ''))
    if params.get('cosyvoice_sft_roles'):
        winobj.sft_roles.setPlainText(params.get('cosyvoice_sft_roles', ''))
    seed_val = params.get('cosyvoice_seed', 0)
    if seed_val:
        winobj.seed.setText(str(seed_val))

    winobj.save.clicked.connect(save)
    winobj.test.clicked.connect(test)
    winobj.show()
