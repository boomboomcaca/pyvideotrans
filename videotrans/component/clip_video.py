import os
from pathlib import Path
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QRunnable, Signal, QObject, QUrl, Slot, QSize, QThread
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QFileDialog, QListWidget, QListWidgetItem, QCheckBox,
    QComboBox, QPlainTextEdit, QScroller
)

from videotrans.configure.config import ROOT_DIR, tr, settings, HOME_DIR
# 全局输出文件夹
from videotrans.util import tools

output_folder = HOME_DIR



class Signals(QObject):
    progress = Signal(str)
    finished = Signal()
    load_error = Signal(str)

class ClipTask(QRunnable):
    def __init__(self, video_path, sub, line_num, subtitle_name, signals, mode,video_info=None, precise=False):
        super().__init__()
        self.video_path = video_path
        self.sub = sub
        self.line_num = line_num
        self.subtitle_name = subtitle_name
        self.signals = signals
        self.mode = mode
        self.video_info=video_info
        # 合并模式下需精确切割：True 时用重新编码代替 -c copy
        self.precise = precise

    def run(self):
        try:
            start_time = self.sub["startraw"].replace(',','.')
            duration = (self.sub["end_time"] - self.sub["start_time"])/1000.0
            if duration<0.1:
                self.signals.progress.emit(f"Failed:{self.line_num} : {duration}s")
                return
            output_dir = f'{output_folder}/{self.subtitle_name}-clip'
            os.makedirs(output_dir, exist_ok=True)

            # 合并(precise)模式：用重新编码可从任意时间点精确下刀，
            # 避免 -c copy 回退到关键帧导致相邻片段内容重叠重复
            if self.precise:
                from videotrans.util.help_ffmpeg import get_video_codec
                encoder = "libx264" if settings.get('force_lib') else get_video_codec(264)
                if encoder == "h264_nvenc":
                    vcodec = ["-c:v", "h264_nvenc", "-cq", "18", "-preset", "p4", "-pix_fmt", "yuv420p"]
                elif encoder == "h264_qsv":
                    vcodec = ["-c:v", "h264_qsv", "-global_quality", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
                elif encoder == "h264_amf":
                    vcodec = ["-c:v", "h264_amf", "-rc", "cqp", "-qp_p", "18", "-qp_i", "18", "-quality", "balanced", "-pix_fmt", "yuv420p"]
                elif encoder == "h264_videotoolbox":
                    vcodec = ["-c:v", "h264_videotoolbox", "-q:v", "75", "-pix_fmt", "yuv420p"]
                elif encoder == "h264_vaapi":
                    vcodec = ["-c:v", "h264_vaapi", "-qp", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
                else:
                    vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
                acodec = ["-c:a", "aac", "-b:a", "192k"]
            else:
                vcodec = ["-c:v", "copy", "-crf", "18"]
                acodec = ["-c:a", "copy"]

            if self.mode == 0:  # 默认
                output_path = os.path.join(output_dir, f"{self.line_num}.mp4")
                cmd = ["-y", "-ss", str(start_time), "-t", str(duration), "-i", self.video_path]
                if self.video_info['streams_audio']>0:
                    cmd += vcodec + acodec
                else:
                    cmd += ["-an"] + vcodec
                cmd += [output_path]
                tools.runffmpeg(cmd, force_cpu=True)
            elif self.mode == 1:  # 仅视频
                output_path = os.path.join(output_dir, f"{self.line_num}.mp4")
                cmd = ["-y", "-ss", str(start_time), "-t", str(duration),
                       "-i", self.video_path, "-an"] + vcodec + [output_path]
                tools.runffmpeg(cmd, force_cpu=True)
            elif self.mode == 2:  # 仅音频
                output_path = os.path.join(output_dir, f"{self.line_num}.wav")
                cmd = ["-y", "-ss", str(start_time), "-t", str(duration),
                       "-i", self.video_path, "-vn", "-c:a", "pcm_s16le", output_path]
                tools.runffmpeg(cmd, force_cpu=True)
            elif self.mode == 3:  # 分离
                # 无声视频
                video_path_out = os.path.join(output_dir, f"{self.line_num}.mp4")
                cmd_video = ["-y", "-ss", str(start_time), "-t", str(duration),
                             "-i", self.video_path, "-an"] + vcodec + [video_path_out]
                tools.runffmpeg(cmd_video, force_cpu=True)

                # 音频
                if self.video_info['streams_audio']>0:
                    audio_path_out = os.path.join(output_dir, f"{self.line_num}.wav")
                    cmd_audio = ["-y", "-ss", str(start_time), "-t", str(duration),
                                 "-i", self.video_path, "-vn", "-c:a", "pcm_s16le", audio_path_out]
                    tools.runffmpeg(cmd_audio, force_cpu=True)

            self.signals.progress.emit(f"Completed: {self.line_num}Line")
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode() if e.stderr else str(e)
            self.signals.progress.emit(f"Failed: {self.line_num}: {error_msg}")
        except Exception as e:
            self.signals.progress.emit(f"Failed: {self.line_num}: {str(e)}")



class ClipVideoWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("appTitle"))
        self.resize(1000, 600)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint)

        self.video_path = None
        self.subtitle_path = None
        self.subtitles = None
        self.subtitle_name = None
        self.selected_lines = []
        self.thread_pool = QThreadPool()
        self.is_clipping = False
        self.signals = Signals()
        self.signals.progress.connect(self.update_progress)
        self.signals.finished.connect(self.clipping_finished)
        self.signals.load_error.connect(self.on_load_error)
        self.total_clips = 0
        self.completed_clips = 0
        self.failed_clips = []
        self.open_button = None
        self.active_tasks = 0
        # 合并相关状态
        self.is_merge = False
        self.clip_mode = 0
        self.merge_started = False
        self.merge_worker = None
        self.merge_groups = []  # 合并模式下勾选行的连续分组
        self.merge_keep = []    # 合并模式下需保留的时间区间[(start_ms,end_ms)]

        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()

        # 文件选择
        file_layout = QHBoxLayout()
        self.video_label = QLabel(tr("noVideoSelected"))
        video_btn = QPushButton(tr("selectVideoToEdit"))
        video_btn.clicked.connect(self.select_video)
        video_btn.setMinimumSize(QSize(200, 35))
        video_btn.setCursor(Qt.PointingHandCursor)
        file_layout.addWidget(video_btn)
        file_layout.addWidget(self.video_label)

        self.subtitle_label = QLabel(tr('noSubtitleSelected'))
        subtitle_btn = QPushButton(tr('selectCorrespondingSubtitle'))
        subtitle_btn.setMinimumSize(QSize(200, 35))
        subtitle_btn.clicked.connect(self.select_subtitle)
        subtitle_btn.setCursor(Qt.PointingHandCursor)
        file_layout.addWidget(subtitle_btn)
        file_layout.addWidget(self.subtitle_label)

        # 输出模式下拉列表
        self.output_mode = QComboBox()
        self.output_mode.addItems([
            tr("optionDefault"),
            tr("optionVideoOnly"),
            tr("optionAudioOnly"),
            tr("optionSeparateAV")
        ])
        file_layout.addWidget(self.output_mode)

        # 合并为一个文件
        self.merge_checkbox = QCheckBox(tr("mergeIntoOne"))
        self.merge_checkbox.setCursor(Qt.PointingHandCursor)
        self.merge_checkbox.setToolTip(tr("mergeIntoOneTip"))
        file_layout.addWidget(self.merge_checkbox)

        file_layout.addStretch()
        layout.addLayout(file_layout)



        # 批量选择按钮
        batch_layout = QHBoxLayout()
        self.select_all_btn = QPushButton(tr("selectAll"))
        self.select_all_btn.clicked.connect(self.select_all)
        self.select_all_btn.setCursor(Qt.PointingHandCursor)
        self.select_all_btn.setVisible(False)
        batch_layout.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton(tr("deselectAll"))
        self.deselect_all_btn.clicked.connect(self.deselect_all)
        self.deselect_all_btn.setCursor(Qt.PointingHandCursor)
        self.deselect_all_btn.setVisible(False)
        batch_layout.addWidget(self.deselect_all_btn)

        self.invert_btn = QPushButton(tr("invertSelection"))
        self.invert_btn.clicked.connect(self.invert_selection)
        self.invert_btn.setVisible(False)
        self.invert_btn.setCursor(Qt.PointingHandCursor)
        batch_layout.addWidget(self.invert_btn)
        batch_layout.addStretch()
        layout.addLayout(batch_layout)

        # 字幕列表
        self.subtitle_list = QListWidget()
        QScroller.ungrabGesture(self.subtitle_list.viewport())
        self.subtitle_list.setAutoScroll(False)
        layout.addWidget(self.subtitle_list)

        # 底部按钮
        btn_layout = QHBoxLayout()

        self.clip_btn = QPushButton(tr("startEditing"))
        self.clip_btn.setCursor(Qt.PointingHandCursor)
        self.clip_btn.setMinimumSize(QSize(200, 35))
        self.clip_btn.clicked.connect(self.start_clipping)
        btn_layout.addWidget(self.clip_btn)

        self.clear_btn = QPushButton(tr("clearSelection"))
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setMaximumWidth(150)
        self.clear_btn.clicked.connect(self.clear_all)
        btn_layout.addWidget(self.clear_btn)
        
        self.open_button = QPushButton(tr("openOutputDirectory"))
        self.open_button.setMaximumWidth(200)
        self.open_button.setCursor(Qt.PointingHandCursor)
        self.open_button.clicked.connect(self.open_output_folder)
        self.open_button.hide()
        btn_layout.addWidget(self.open_button)
        

        layout.addLayout(btn_layout)

        # 进度标签
        self.progress_label = QPlainTextEdit("")
        self.progress_label.setStyleSheet('color:#2196f3;font-size:14px')
        self.progress_label.setReadOnly(True)
        self.progress_label.setFixedHeight(80)
        layout.addWidget(self.progress_label)
        self.setWindowIcon(QIcon(f"{ROOT_DIR}/videotrans/styles/icon.ico"))
        self.setLayout(layout)

    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("selectVideo"), settings.get('last_opendir',''), "Video Files (*.mp4 *.avi *.mkv)")
        if path:
            self.video_path = path
            self.video_label.setText(os.path.basename(path))
            settings['last_opendir']=Path(path).parent.as_posix()

    def select_subtitle(self):
        global output_folder
        path, _ = QFileDialog.getOpenFileName(self, tr("selectSubtitle"), settings.get('last_opendir',''), "Subtitle Files (*.srt *.ass *.vtt)")
        if path:
            self.progress_label.setPlainText(tr("renderingSubtitles"))
            self.subtitle_list.clear()
            output_folder=Path(path).parent.as_posix()
            self.subtitle_path = path
            self.subtitle_name = Path(path).name
            self.subtitle_label.setText(self.subtitle_name)


            self.subtitles = tools.get_subtitle_from_srt(self.subtitle_path)  # Reload if needed
            for i, it in enumerate(self.subtitles):
                item = QListWidgetItem()
                check = QCheckBox(f"第{i+1}行 [{(it['end_time']-it['start_time'])/1000.0}s] {it['startraw']}->{it['endraw']}  {it['text']}")
                self.subtitle_list.addItem(item)
                self.subtitle_list.setItemWidget(item, check)
                item.setSizeHint(check.sizeHint() + QSize(0, 10))  # 增加垂直间距
            self.progress_label.setPlainText(f"{tr('renderCompleteOutputTo')}:{output_folder}/{self.subtitle_name}-clip")
            self.select_all_btn.setVisible(True)
            self.deselect_all_btn.setVisible(True)
            self.invert_btn.setVisible(True)
            settings['last_opendir']=Path(self.subtitle_path).parent.as_posix()

    @Slot(str)
    def on_load_error(self, error):
        self.progress_label.setPlainText(f"{tr('subtitleRenderError')}: {error}")

    def select_all(self):
        for i in range(self.subtitle_list.count()):
            item = self.subtitle_list.item(i)
            check = self.subtitle_list.itemWidget(item)
            check.setChecked(True)

    def deselect_all(self):
        for i in range(self.subtitle_list.count()):
            item = self.subtitle_list.item(i)
            check = self.subtitle_list.itemWidget(item)
            check.setChecked(False)

    def invert_selection(self):
        for i in range(self.subtitle_list.count()):
            item = self.subtitle_list.item(i)
            check = self.subtitle_list.itemWidget(item)
            check.setChecked(not check.isChecked())

    def clear_all(self):
        self.video_path = None
        self.subtitle_path = None
        self.subtitles = None
        self.subtitle_name = None
        self.selected_lines = []
        self.video_label.setText(tr("noVideoSelected"))
        self.subtitle_label.setText(tr("noSubtitleSelected"))
        self.subtitle_list.clear()
        self.progress_label.setPlainText("")
        self.clip_btn.setText(tr("startEditing"))
        self.is_clipping = False
        self.total_clips = 0
        self.completed_clips = 0
        self.failed_clips = []
        self.output_mode.setCurrentIndex(0)
        self.merge_checkbox.setChecked(False)
        self.is_merge = False
        self.merge_started = False
        self.active_tasks = 0
        self.open_button.hide()
        self.select_all_btn.setVisible(False)
        self.deselect_all_btn.setVisible(False)
        self.invert_btn.setVisible(False)

    def start_clipping(self):
        if self.is_clipping:
            self.stop_clipping()
            return

        if not self.video_path or not self.subtitle_name:
            self.progress_label.setPlainText(tr("promptSelectVideoAndSubtitle"))
            return

        self.selected_lines = []
        for i in range(self.subtitle_list.count()):
            item = self.subtitle_list.item(i)
            check = self.subtitle_list.itemWidget(item)
            if check.isChecked():
                self.selected_lines.append(i + 1)  # 1-based

        if not self.selected_lines:
            self.progress_label.setPlainText(tr("promptSelectAtLeastOneSubtitle"))
            return

        mode = self.output_mode.currentIndex()
        self.clip_mode = mode
        self.is_merge = self.merge_checkbox.isChecked()
        self.merge_started = False
        # 合并模式：保留整段视频，仅挖掉没勾选的字幕行；保留区间需视频时长，放到 Worker 里算
        self.merge_keep = []

        self.is_clipping = True
        self.clip_btn.setText(tr("stopImmediately"))
        self.completed_clips = 0
        self.failed_clips = []
        self.open_button.show()
        if self.is_merge:
            # 保留区间数由 Worker 算出后再更新计数
            self.total_clips = 0
            self.active_tasks = 0
            self.progress_label.setPlainText(tr("mergingClips"))
        else:
            self.total_clips = len(self.selected_lines)
            self.active_tasks = self.total_clips
            self.progress_label.setPlainText(f"Total:{self.total_clips}")
        task = Worker(parent=self,mode=mode)
        task.uito.connect(self.update_progress)
        task.start()

    @staticmethod
    def _group_consecutive(lines):
        """将行号列表按连续性分组：[1,2,3,5,6,10] -> [[1,2,3],[5,6],[10]]。
        每组会被合并为一段连续视频(保留组内字幕之间的间隙)。"""
        groups = []
        for n in sorted(lines):
            if groups and n == groups[-1][-1] + 1:
                groups[-1].append(n)
            else:
                groups.append([n])
        return groups

    def _compute_keep_ranges(self, total_ms):
        """合并模式：保留每个勾选连续段 + 该段说完后的停顿(到下一行开始)；
        没勾的行连同它后面的停顿一起挖掉。第一段并入片头，段尾若是最后一行则并入片尾。
        返回保留的时间区间 [(start_ms, end_ms), ...]。"""
        groups = self._group_consecutive(self.selected_lines)
        keep = []
        total_lines = len(self.subtitles)
        for gi, group in enumerate(groups):
            s = self.subtitles[group[0] - 1]['start_time']
            last = group[-1]  # 段尾行号(1-based)
            if last < total_lines:
                # 段尾延伸到下一行(没勾行)的开始，保留本段说完后的停顿
                e = self.subtitles[last]['start_time']
            else:
                e = total_ms    # 段尾是最后一行，并入片尾
            if gi == 0:
                s = 0           # 第一段并入片头
            keep.append((s, e))
        return keep



    def stop_clipping(self):
        self.thread_pool.clear()
        self.is_clipping = False
        self.merge_started = False
        self.clip_btn.setText(tr("startEditing"))
        self.progress_label.setPlainText(tr("statusStopped"))
        self.active_tasks = 0

    def update_progress(self, message):
        if message.startswith("Error:"):
            self.stop_clipping()
            return
        if  message.startswith("Completed:"):
            self.completed_clips += 1
        elif message.startswith("Failed:"):
            self.failed_clips.append(message)
        self.active_tasks -= 1
        self.progress_label.setPlainText(
            f" {self.completed_clips}/{self.total_clips},  "
            f"Error: {len(self.failed_clips)}\n" + "\n".join(self.failed_clips)
        )
        if self.active_tasks <= 0 and self.is_clipping and not self.merge_started:
            # 所有片段剪辑完成：若勾选合并且至少有一个成功片段，则启动合并
            if self.is_merge and self.completed_clips > 0:
                self.merge_started = True
                self._start_merge()
            else:
                self.signals.finished.emit()

    def _start_merge(self):
        self.progress_label.appendPlainText(tr("mergingClips"))
        self.merge_worker = MergeWorker(parent=self, mode=self.clip_mode)
        self.merge_worker.uito.connect(self.on_merge_done)
        self.merge_worker.start()

    @Slot(str)
    def on_merge_done(self, message):
        if message.startswith("MergeFailed:"):
            self.progress_label.appendPlainText(f"{tr('mergeFailed')}: {message[len('MergeFailed:'):]}")
        else:
            self.progress_label.appendPlainText(tr("mergeComplete"))
        self.signals.finished.emit()

    def _do_merge(self, mode):
        """将已剪辑出的各连续段按顺序合并为单个文件。在后台线程中调用。"""
        output_dir = f'{output_folder}/{self.subtitle_name}-clip'
        # 每个连续勾选段输出为 {段序号}.mp4 / .wav，按序号顺序拼接
        seg_ids = list(range(1, len(self.merge_keep) + 1))
        outputs = []

        def _cleanup(paths):
            # 合并成功后删除中间片段，只保留合并文件
            for p in paths:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

        def _concat_video(seg_names, out_name):
            seg_paths = [os.path.join(output_dir, n) for n in seg_names]
            concat_txt = os.path.join(output_dir, '_merge_video.txt')
            tools.create_concat_txt(seg_paths, concat_txt=concat_txt)
            out_path = os.path.join(output_dir, out_name)
            cmd = ['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt, '-c', 'copy', out_path]
            tools.runffmpeg(cmd, force_cpu=True, cmd_dir=output_dir)
            try:
                os.remove(concat_txt)
            except OSError:
                pass
            outputs.append(out_path)
            _cleanup(seg_paths)

        def _concat_audio(seg_names, out_name):
            seg_paths = [os.path.join(output_dir, n) for n in seg_names]
            concat_txt = os.path.join(output_dir, '_merge_audio.txt')
            tools.create_concat_txt(seg_paths, concat_txt=concat_txt)
            out_path = os.path.join(output_dir, out_name)
            tools.concat_multi_audio(out=out_path, concat_txt=concat_txt)
            try:
                os.remove(concat_txt)
            except OSError:
                pass
            outputs.append(out_path)
            _cleanup(seg_paths)

        if mode in (0, 1):  # 默认/仅视频 -> 合并为一个 mp4
            _concat_video([f"{n}.mp4" for n in seg_ids], "_merged.mp4")
        elif mode == 2:  # 仅音频 -> 合并为一个 wav
            _concat_audio([f"{n}.wav" for n in seg_ids], "_merged.wav")
        elif mode == 3:  # 声画分离 -> 合并出一个无声 mp4 + 一个 wav
            _concat_video([f"{n}.mp4" for n in seg_ids], "_merged.mp4")
            wavs = [f"{n}.wav" for n in seg_ids
                    if os.path.exists(os.path.join(output_dir, f"{n}.wav"))]
            if wavs:
                _concat_audio(wavs, "_merged.wav")

        # 生成并合并字幕文件
        if self.subtitles and self.subtitle_path:
            try:
                from videotrans.task.taskcfg import SrtItem
                from videotrans.util.help_srt import ms_to_time_string, get_srt_from_list
                from videotrans.configure.config import logger

                merged_subtitles = []
                accumulated_time = 0
                line_idx = 1

                for ks, ke in self.merge_keep:
                    for ln in sorted(self.selected_lines):
                        original_sub = self.subtitles[ln - 1]
                        # 只处理落在当前保留区间内的勾选行
                        if not (ks <= original_sub['start_time'] < ke):
                            continue
                        new_start = accumulated_time + (original_sub['start_time'] - ks)
                        new_end = accumulated_time + (original_sub['end_time'] - ks)

                        new_sub = SrtItem(
                            line=line_idx,
                            start_time=new_start,
                            end_time=new_end,
                            text=original_sub['text']
                        )
                        new_sub['startraw'] = ms_to_time_string(ms=new_start)
                        new_sub['endraw'] = ms_to_time_string(ms=new_end)
                        new_sub['time'] = f"{new_sub['startraw']} --> {new_sub['endraw']}"

                        merged_subtitles.append(new_sub)
                        line_idx += 1

                    accumulated_time += (ke - ks)

                srt_path = os.path.join(output_dir, "_merged.srt")
                srt_content = get_srt_from_list(merged_subtitles)
                with open(srt_path, 'w', encoding='utf-8') as f:
                    f.write(srt_content)
                outputs.append(srt_path)

                # 如果原字幕不是 .srt，利用 ffmpeg 转换格式
                orig_suffix = Path(self.subtitle_path).suffix.lower()
                if orig_suffix in ('.vtt', '.ass'):
                    target_sub_path = os.path.join(output_dir, f"_merged{orig_suffix}")
                    try:
                        tools.runffmpeg(['-y', '-i', srt_path, target_sub_path], force_cpu=True)
                        outputs.append(target_sub_path)
                    except Exception as e:
                        logger.warning(f"Failed to convert subtitle to {orig_suffix}: {e}")

                # 删除中间临时产生的片段字幕文件
                tmp_sub_paths = []
                for seg_idx in seg_ids:
                    tmp_sub_paths.append(os.path.join(output_dir, f"{seg_idx}.srt"))
                    if orig_suffix in ('.vtt', '.ass'):
                        tmp_sub_paths.append(os.path.join(output_dir, f"{seg_idx}{orig_suffix}"))
                _cleanup(tmp_sub_paths)

            except Exception as e:
                from videotrans.configure.config import logger
                logger.exception(f"Failed to generate merged subtitle: {e}")

        return outputs

    def clipping_finished(self):
        self.is_clipping = False
        self.merge_started = False
        self.clip_btn.setText(tr("startEditing"))
        self.active_tasks = 0


    def open_output_folder(self):
        output_dir = f'{output_folder}/{self.subtitle_name}-clip'
        QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))

    def _write_clip_subtitle(self, group, file_prefix):
        """为某个片段写入对应的字幕文件。"""
        from videotrans.task.taskcfg import SrtItem
        from videotrans.util.help_srt import ms_to_time_string, get_srt_from_list
        from videotrans.configure.config import logger
        import os

        output_dir = f'{output_folder}/{self.subtitle_name}-clip'
        os.makedirs(output_dir, exist_ok=True)

        first_sub = self.subtitles[group[0] - 1]
        seg_start = first_sub['start_time']

        clip_subs = []
        for line_idx, ln in enumerate(group, start=1):
            original_sub = self.subtitles[ln - 1]
            new_start = original_sub['start_time'] - seg_start
            new_end = original_sub['end_time'] - seg_start

            new_sub = SrtItem(
                line=line_idx,
                start_time=new_start,
                end_time=new_end,
                text=original_sub['text']
            )
            new_sub['startraw'] = ms_to_time_string(ms=new_start)
            new_sub['endraw'] = ms_to_time_string(ms=new_end)
            new_sub['time'] = f"{new_sub['startraw']} --> {new_sub['endraw']}"
            clip_subs.append(new_sub)

        srt_path = os.path.join(output_dir, f"{file_prefix}.srt")
        srt_content = get_srt_from_list(clip_subs)
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(srt_content)

        # 如果原字幕不是 .srt，利用 ffmpeg 转换格式
        orig_suffix = Path(self.subtitle_path).suffix.lower()
        created_paths = [srt_path]
        if orig_suffix in ('.vtt', '.ass'):
            target_sub_path = os.path.join(output_dir, f"{file_prefix}{orig_suffix}")
            try:
                tools.runffmpeg(['-y', '-i', srt_path, target_sub_path], force_cpu=True)
                created_paths.append(target_sub_path)
            except Exception as e:
                logger.warning(f"Failed to convert subtitle to {orig_suffix}: {e}")

        return created_paths

class Worker(QThread):
    uito = Signal(str)

    def __init__(self, *,
        parent:ClipVideoWindow,
        mode=None):
        super().__init__(parent=parent)
        self.parent=parent
        self.mode=mode

    def run(self):
        try:
            video_info=tools.get_video_info(self.parent.video_path)
            if video_info['streams_audio']==0 and self.mode == 2:
                self.uito.emit(f"Error:{tr('errorNoAudioTrackForAudioOnly')}")
                return
            if self.parent.is_merge:
                # 合并模式：保留整段视频，仅挖掉没勾选的字幕行；切出每个保留区间
                from videotrans.util.help_srt import ms_to_time_string
                keep = self.parent._compute_keep_ranges(video_info['time'])
                self.parent.merge_keep = keep
                self.parent.total_clips = len(keep)
                self.parent.active_tasks = len(keep)
                if not keep:
                    self.uito.emit(f"Error:{tr('promptSelectAtLeastOneSubtitle')}")
                    return
                for seg_idx, (ks, ke) in enumerate(keep, start=1):
                    seg_sub = {
                        "startraw": ms_to_time_string(ms=ks),
                        "start_time": ks,
                        "end_time": ke,
                    }
                    task = ClipTask(self.parent.video_path, seg_sub, seg_idx, self.parent.subtitle_name, self.parent.signals, self.mode, video_info, precise=True)
                    self.parent.thread_pool.start(task)
            else:
                for line_num in self.parent.selected_lines:
                    sub = self.parent.subtitles[line_num - 1]
                    task = ClipTask(self.parent.video_path, sub, line_num, self.parent.subtitle_name, self.parent.signals, self.mode, video_info, precise=False)
                    self.parent.thread_pool.start(task)
                    try:
                        self.parent._write_clip_subtitle([line_num], str(line_num))
                    except Exception as e:
                        from videotrans.configure.config import logger
                        logger.warning(f"Failed to write clip subtitle: {e}")

        except Exception as e:
            self.uito.emit(f"Error:{e}")


class MergeWorker(QThread):
    uito = Signal(str)

    def __init__(self, *,
        parent:ClipVideoWindow,
        mode=None):
        super().__init__(parent=parent)
        self.parent=parent
        self.mode=mode

    def run(self):
        try:
            # 等待线程池中可能仍在收尾的剪辑任务全部结束，确保片段文件已落盘
            self.parent.thread_pool.waitForDone()
            outputs = self.parent._do_merge(self.mode)
            self.uito.emit(f"MergeCompleted:{len(outputs)}")
        except Exception as e:
            self.uito.emit(f"MergeFailed:{e}")