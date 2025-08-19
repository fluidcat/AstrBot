import os
import uuid

from astrbot.core import logger
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import STTProvider
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.dify_api_client import DifyAPIClient
from astrbot.core.utils.io import download_file
from astrbot.core.utils.tencent_record_helper import tencent_silk_to_wav


@register_provider_adapter(
    "dify_stt", "Dify WorkFlow STT", provider_type=ProviderType.SPEECH_TO_TEXT
)
class DifySTT(STTProvider):
    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
    ) -> None:
        super().__init__(provider_config, provider_settings)
        self.provider_config = provider_config
        self.provider_settings = provider_settings

        self.api_key = provider_config.get("dify_api_key", "")
        if not self.api_key:
            raise Exception("Dify API Key 不能为空。")
        api_base = provider_config.get("dify_api_base", "https://api.dify.ai/v1")
        self.timeout = provider_config.get("timeout", 120)
        if isinstance(self.timeout, str):
            self.timeout = int(self.timeout)

        self.workflow_output_key = provider_config.get(
            "workflow_output_key", "stt_output"
        )
        self.workflow_iutput_key = provider_config.get(
            "workflow_iutput_key", "stt_input"
        )

        self.dify_client = DifyAPIClient(self.api_key, api_base)

    async def _is_silk_file(self, file_path):
        silk_header = b"SILK"
        with open(file_path, "rb") as f:
            file_header = f.read(8)

        if silk_header in file_header:
            return True
        else:
            return False

    async def get_text(self, audio_url: str) -> str:
        """only supports mp3, mp4, mpeg, m4a, wav, webm"""
        is_tencent = False

        if audio_url.startswith("http"):
            if "multimedia.nt.qq.com.cn" in audio_url:
                is_tencent = True

            name = str(uuid.uuid4())
            temp_dir = os.path.join(get_astrbot_data_path(), "temp")
            path = os.path.join(temp_dir, name)
            await download_file(audio_url, path)
            audio_url = path

        if not os.path.exists(audio_url):
            raise FileNotFoundError(f"文件不存在: {audio_url}")

        if audio_url.endswith(".amr") or audio_url.endswith(".silk") or is_tencent:
            is_silk = await self._is_silk_file(audio_url)
            if is_silk:
                logger.info("Converting silk file to wav ...")
                temp_dir = os.path.join(get_astrbot_data_path(), "temp")
                output_path = os.path.join(temp_dir, str(uuid.uuid4()) + ".wav")
                await tencent_silk_to_wav(audio_url, output_path)
                audio_url = output_path

        result = await self.audio_acr(audio_url, "astrbot_stt")
        return result

    async def audio_acr(self, file_path: str, session_id: str) -> str:
        file_json = await self.dify_client.file_upload(file_path, session_id)
        file_id = file_json.get("id", "")
        if not file_id:
            raise Exception("dify stt: 上传语音文件失败.")
        result = ""
        async for chunk in self.dify_client.workflow_run(
            inputs={
                self.workflow_iutput_key: {
                    "type": "audio",
                    "transfer_method": "local_file",
                    "upload_file_id": file_id,
                }
            },
            user=session_id,
            timeout=self.timeout,
        ):
            match chunk["event"]:
                case "workflow_started":
                    logger.info(
                        f"Dify 工作流(ID: {chunk['workflow_run_id']})开始运行。"
                    )
                case "node_finished":
                    logger.debug(
                        f"Dify 工作流节点(ID: {chunk['data']['node_id']} Title: {chunk['data'].get('title', '')})运行结束。"
                    )
                case "workflow_finished":
                    logger.info(f"Dify 工作流(ID: {chunk['workflow_run_id']})运行结束")
                    logger.debug(f"Dify 工作流结果：{chunk}")
                    if chunk["data"]["error"]:
                        logger.error(f"Dify 工作流出现错误：{chunk['data']['error']}")
                        raise Exception(
                            f"Dify 工作流出现错误：{chunk['data']['error']}"
                        )
                    if self.workflow_output_key not in chunk["data"]["outputs"]:
                        raise Exception(
                            f"Dify 工作流的输出不包含指定的键名：{self.workflow_output_key}"
                        )
                    result = chunk

        return result.get("data", {}).get("outputs", {}).get(self.workflow_output_key, "")
