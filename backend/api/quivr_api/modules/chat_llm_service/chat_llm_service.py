import datetime
from uuid import UUID, uuid4

from quivr_core.chat import ChatHistory as ChatHistoryCore
from quivr_core.chat_llm import ChatLLM
from quivr_core.config import LLMEndpointConfig
from quivr_core.llm.llm_endpoint import LLMEndpoint
from quivr_core.models import ChatLLMMetadata, ParsedRAGResponse, RAGResponseMetadata

from quivr_api.logger import get_logger
from quivr_api.models.settings import settings
from quivr_api.modules.brain.service.utils.format_chat_history import (
    format_chat_history,
)
from quivr_api.modules.chat.controller.chat.utils import (
    compute_cost,
    find_model_and_generate_metadata,
    update_user_usage,
)
from quivr_api.modules.chat.dto.inputs import CreateChatHistory
from quivr_api.modules.chat.dto.outputs import GetChatHistoryOutput
from quivr_api.modules.chat.service.chat_service import ChatService
from quivr_api.modules.models.service.model_service import ModelService
from quivr_api.modules.user.entity.user_identity import UserIdentity
from quivr_api.modules.user.service.user_usage import UserUsage
from quivr_api.packages.utils.uuid_generator import generate_uuid_from_string

logger = get_logger(__name__)


class ChatLLMService:
    def __init__(
        self,
        current_user: UserIdentity,
        model_name: str,
        chat_id: UUID,
        chat_service: ChatService,
        model_service: ModelService,
    ):
        # Services
        self.chat_service = chat_service
        self.model_service = model_service

        # Base models
        self.current_user = current_user
        self.chat_id = chat_id

        # check at init time
        self.model_to_use = self.check_and_update_user_usage(
            self.current_user, model_name
        )

    def _build_chat_history(
        self,
        history: list[GetChatHistoryOutput],
    ) -> ChatHistoryCore:
        transformed_history = format_chat_history(history)
        chat_history = ChatHistoryCore(brain_id=None, chat_id=self.chat_id)

        [chat_history.append(m) for m in transformed_history]
        return chat_history

    def build_llm(self) -> ChatLLM:
        ollama_url = (
            settings.ollama_api_base_url
            if settings.ollama_api_base_url
            and self.model_to_use.name.startswith("ollama")
            else None
        )

        chat_llm = ChatLLM(
            llm=LLMEndpoint.from_config(
                LLMEndpointConfig(
                    model=self.model_to_use.name,
                    llm_base_url=ollama_url,
                    llm_api_key="abc-123" if ollama_url else None,
                    temperature=(LLMEndpointConfig.model_fields["temperature"].default),
                    max_input=self.model_to_use.max_input,
                    max_tokens=self.model_to_use.max_output,
                ),
            )
        )
        return chat_llm

    def check_and_update_user_usage(self, user: UserIdentity, model_name: str):
        """Check user limits and raises if user reached his limits:
        1. Raise if one of the conditions :
           - User doesn't have access to brains
           - Model of brain is not is user_settings.models
           - Latest sum_30d(user_daily_user) < user_settings.max_monthly_usage
           - Check sum(user_settings.daily_user_count)+ model_price <  user_settings.monthly_chat_credits
        2. Updates user usage
        """
        # TODO(@aminediro) : THIS is bug prone, should retrieve it from DB here
        user_usage = UserUsage(id=user.id, email=user.email)
        user_settings = user_usage.get_user_settings()
        all_models = user_usage.get_models()

        # TODO(@aminediro): refactor this function
        model_to_use = find_model_and_generate_metadata(
            model_name,
            user_settings,
            all_models,
        )
        cost = compute_cost(model_to_use, all_models)
        # Raises HTTP if user usage exceeds limits
        update_user_usage(user_usage, user_settings, cost)  # noqa: F821
        return model_to_use

    def save_answer(self, question: str, answer: ParsedRAGResponse):
        logger.info(
            f"Saving answer for chat {self.chat_id} with model {self.model_to_use.name}"
        )
        logger.info(answer)
        return self.chat_service.update_chat_history(
            CreateChatHistory(
                **{
                    "chat_id": self.chat_id,
                    "user_message": question,
                    "assistant": answer.answer,
                    "brain_id": None,
                    "prompt_id": None,
                    "metadata": answer.metadata.model_dump() if answer.metadata else {},
                }
            )
        )

    async def generate_answer(
        self,
        question: str,
    ):
        logger.info(
            f"Creating question for chat {self.chat_id} with model {self.model_to_use.name} "
        )
        chat_llm = self.build_llm()
        history = await self.chat_service.get_chat_history(self.chat_id)
        model_metadata = await self.model_service.get_model(self.model_to_use.name)
        #  Format the history, sanitize the input
        chat_history = self._build_chat_history(history)

        parsed_response = chat_llm.answer(question, chat_history)

        if parsed_response.metadata:
            # TODO: check if this is the right way to do it
            parsed_response.metadata.metadata_model = ChatLLMMetadata(
                name=self.model_to_use.name,
                description=model_metadata.description,
                image_url=model_metadata.image_url,
                display_name=model_metadata.display_name,
                brain_id=str(generate_uuid_from_string(self.model_to_use.name)),
                brain_name=self.model_to_use.name,
            )

        # Save the answer to db
        new_chat_entry = self.save_answer(question, parsed_response)

        # Format output to be correct
        return GetChatHistoryOutput(
            **{
                "chat_id": self.chat_id,
                "user_message": question,
                "assistant": parsed_response.answer,
                "message_time": new_chat_entry.message_time,
                "prompt_title": None,
                "brain_name": None,
                "message_id": new_chat_entry.message_id,
                "brain_id": None,
                "metadata": (
                    parsed_response.metadata.model_dump()
                    if parsed_response.metadata
                    else {}
                ),
            }
        )

    async def generate_answer_stream(
        self,
        question: str,
    ):
        logger.info(
            f"Creating question for chat {self.chat_id} with model {self.model_to_use.name} "
        )
        # Build the rag config
        chat_llm = self.build_llm()

        # Get model metadata
        model_metadata = await self.model_service.get_model(self.model_to_use.name)
        # Get chat history
        history = await self.chat_service.get_chat_history(self.chat_id)
        #  Format the history, sanitize the input
        chat_history = self._build_chat_history(history)

        full_answer = ""

        message_metadata = {
            "chat_id": self.chat_id,
            "message_id": uuid4(),  # do we need it ?,
            "user_message": question,  # TODO: define result
            "message_time": datetime.datetime.now(),  # TODO: define result
            "prompt_title": None,
            "brain_name": None,
            "brain_id": None,
        }

        async for response in chat_llm.answer_astream(question, chat_history):
            # Format output to be correct servicedf;j
            if not response.last_chunk:
                streamed_chat_history = GetChatHistoryOutput(
                    assistant=response.answer,
                    metadata=response.metadata.model_dump(),
                    **message_metadata,
                )
                full_answer += response.answer
                yield f"data: {streamed_chat_history.model_dump_json()}"
            if response.last_chunk and full_answer == "":
                full_answer += response.answer

        # For last chunk  parse the sources, and the full answer
        streamed_chat_history = GetChatHistoryOutput(
            assistant=full_answer,
            metadata=response.metadata.model_dump(),
            **message_metadata,
        )

        metadata = RAGResponseMetadata(**streamed_chat_history.metadata)  # type: ignore
        metadata.metadata_model = ChatLLMMetadata(
            name=self.model_to_use.name,
            description=model_metadata.description,
            image_url=model_metadata.image_url,
            display_name=model_metadata.display_name,
            brain_id=str(generate_uuid_from_string(self.model_to_use.name)),
            brain_name=self.model_to_use.name,
        )
        streamed_chat_history.metadata = metadata.model_dump()

        logger.info("Last chunk before saving")
        self.save_answer(
            question,
            ParsedRAGResponse(
                answer=full_answer,
                metadata=metadata,
            ),
        )
        yield f"data: {streamed_chat_history.model_dump_json()}"
