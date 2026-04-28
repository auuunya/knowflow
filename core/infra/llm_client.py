import logging

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


# LLM 调用层：封装统一链路，记录请求和响应摘要
class LLMClient:
    def __init__(self, config):
        self.config = config
        self._llm = ChatOpenAI(
            base_url=config.llm_base_url,
            model=config.model_name,
            api_key=config.llm_api_key,
            temperature=config.temperature,
            timeout=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
        )

    def run_chain(self, prompt, variables):
        """运行单次 chain，记录输入输出以便追踪。"""
        prompt_keys = list(variables.keys())
        logger.info("LLM 调用开始: model=%s, keys=%s", self.config.model_name, prompt_keys)
        for key, value in variables.items():
            logger.debug("  变量 %s 长度=%s", key, len(str(value)))

        try:
            chain = prompt | self._llm
            response = chain.invoke(variables)
            content = response.content
            logger.info("LLM 调用成功: model=%s, response_length=%s", self.config.model_name, len(content))
            logger.debug("LLM 响应摘要: %s", content[:200] + ("..." if len(content) > 200 else ""))
            return content
        except Exception as exc:
            logger.exception("LLM 调用失败: %s", exc)
            raise
