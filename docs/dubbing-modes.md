# 配音方式：Remote / In-browser / Off

工程设置里的 `dubbing_mode` 只有三个值：

```text
remote   远端 Qwen TTS；支持声音克隆、远端预制音色，按量计费
browser  浏览器本地 Piper；FREE，不克隆
off      不配音；只做转写/翻译/字幕，原声保持不变
```

旧的 `enable_dubbing` 布尔值已经删除，代码不保留双读兼容。

## 迁移

一次性迁移所有 `backend/data/*/project.json`：

```bash
cd backend
../.venv/Scripts/python scripts/migrate_dubbing_mode.py
```

规则：

```text
enable_dubbing=false -> off
enable_dubbing=true  -> remote
enable_voice_clone   -> 保留不动
```

工程设置存在 `project.json`，不在 SQLite。SQLite 只存账号、session、工程归属。

## UI

- 配音方式三选一：Remote / In-browser / Off
- In-browser 和 BGM 分离 In-browser 右上角有 `FREE`
- Off：隐藏 BGM 分离、声音克隆、智能选材、音色
- In-browser：隐藏声音克隆、智能选材；显示 `预制音色 · <目标语言>` 音色池
- 音色池列出 12 个、超出显示 `+N`；多说话人首次生成时自动给每位说话人分配不同 voice
- 生成后可在每一行的菜单里改这一句的 voice，或恢复跟随说话人
- 没有本地 Piper 音色的语言，In-browser 置灰并显示原因

## 本地音色

浏览器端使用 `@diffusionstudio/vits-web`（标准 Piper voice，ONNX + WASM）。

当前默认 voice：

| 语言 | voice |
|---|---|
| Chinese | `zh_CN-huayan-medium` |
| English | `en_US-hfc_female-medium` |
| French | `fr_FR-siwis-medium` |
| German | `de_DE-thorsten-medium` |
| Italian | `it_IT-riccardo-x_low` |
| Portuguese | `pt_BR-faber-medium` |
| Vietnamese | `vi_VN-vais1000-medium` |

日语、韩语、印尼语当前标准 Piper 目录里没有可用 voice，因此 In-browser 禁用；以后接 Piper Plus 再补。

浏览器配音在初始化页的 TTS 阶段自动执行：先分配音色，再逐句合成、上传；首次合成时下载并缓存 voice 模型（几十 MB），完成后才进入编辑器。多说话人分配到的其他 voice 在第一次用到时下载。

## 音频路径

浏览器生成 WAV 后上传：

```text
POST /api/projects/{id}/browser-tts/{segment_id}
```

后端转成：

```text
backend/data/<id>/tts/<segment_id>.mp3
```

并写回：

- `actual_duration`
- `tts_fp`

之后混音、时间线、导出完全复用 Remote TTS 的现有路径。

In-browser 不做声音克隆，也不支持 Smart selection。

## 失败与重试

- 浏览器合成失败：界面提示具体句号，已上传的句子不会丢
- 切换 voice 或目标语言：旧指纹失效，重新补配音即可
- 没有本地 voice：不能选 In-browser，提示改用 Remote / Off
