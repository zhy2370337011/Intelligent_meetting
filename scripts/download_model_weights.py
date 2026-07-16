"""预下载智能会议小模型权重。

用途：
- 本地或算力服务器首次启动模型服务前，先运行该脚本把 ModelScope 权重拉到缓存。
- 主后端不直接依赖这些权重；权重只服务于 `backend/model_services/local_models_api.py`。

注意：
- `fsmn-vad` 是 FunASR 短名，通常会在首次 AutoModel 加载时自动解析下载。
- CAM++ 和 3D-Speaker/diarization 建议使用 ModelScope 完整模型 ID，便于内网镜像和离线部署。
- Qwen3-ForcedAligner-0.6B 当前建议独立部署 GPU 服务；如果官方包/镜像确定后，可把下载逻辑补到本脚本。
"""

from __future__ import annotations

import os
from pathlib import Path


def _download_with_modelscope(model_id: str) -> None:
    """使用 ModelScope 下载模型快照。

    这里单独封装是为了让缺失 modelscope 时给出明确提示，而不是在脚本入口处直接崩溃。
    """

    from modelscope import snapshot_download  # type: ignore

    print(f"[modelscope] downloading {model_id} ...")
    path = snapshot_download(model_id)
    print(f"[modelscope] ready: {path}")


def main() -> None:
    """按环境变量下载可预拉取的模型权重。"""

    model_ids = [
        os.getenv("CAMPP_MODEL_ID", "iic/speech_campplus_sv_zh-cn_16k-common"),
        os.getenv("DIARIZATION_MODEL_ID", "iic/speech_campplus_speaker-diarization_common"),
        # 3D-Speaker diarization 的 ModelScope pipeline 会在内部调用以下三个组件模型。
        # 显式预下载它们，是为了服务器离线部署时避免首次请求才联网拉权重。
        "damo/speech_campplus_sv_zh-cn_16k-common",
        "damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "damo/speech_campplus-transformer_scl_zh-cn_16k-common",
    ]

    # 如果部署人员把 FSMN_VAD_MODEL_ID 配成完整 iic/... 模型，也一起预下载；
    # 如果仍使用短名 fsmn-vad，则交给 FunASR AutoModel 首次加载时解析。
    vad_model = os.getenv("FSMN_VAD_MODEL_ID", "fsmn-vad")
    if "/" in vad_model:
        model_ids.append(vad_model)

    for model_id in dict.fromkeys(model_ids):
        if model_id:
            _download_with_modelscope(model_id)

    cache_home = Path(os.getenv("MODELSCOPE_CACHE", Path.home() / ".cache" / "modelscope"))
    print(f"模型缓存目录：{cache_home}")


if __name__ == "__main__":
    main()
