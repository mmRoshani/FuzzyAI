import logging
from typing import Any, Final, Optional, Type

from pydantic import BaseModel, Field

from fuzzyai.consts import DEFAULT_OPEN_SOURCE_MODEL
from fuzzyai.enums import LLMRole
from fuzzyai.handlers.attacks.actor_attack.prompts import (ACTORS_GENERATION_PROMPT, BEHAVIOR_EXTRACTION_PROMPT,
                                                           QUESTIONS_GENERATION_PROMPT)
from fuzzyai.handlers.attacks.actor_attack.utils import generate_model_error
from fuzzyai.handlers.attacks import base as base_module
from fuzzyai.handlers.attacks.base import (BaseAttackTechniqueHandler, BaseAttackTechniqueHandlerException,
                                           attack_handler_fm)
from fuzzyai.handlers.attacks.enums import FuzzerAttackMode
from fuzzyai.handlers.attacks.models import AttackResultEntry
from fuzzyai.llm.models import BaseLLMProviderResponse
from fuzzyai.llm.providers.base import BaseLLMMessage, BaseLLMProvider, BaseLLMProviderException

logger = logging.getLogger(__name__)

DEFAULT_ACTORS_GENERATION_MODEL: Final[str] = DEFAULT_OPEN_SOURCE_MODEL
DEFAULT_BEHAVIOR_EXTRACTION_MODEL: Final[str] = DEFAULT_OPEN_SOURCE_MODEL
DEFAULT_QUESTIONS_GENERATION_MODEL: Final[str] = DEFAULT_OPEN_SOURCE_MODEL

SPLIT_TOKEN: Final[str] = '[SPLIT]'
MODELS_TYPES: Final[list[str]] = ["behavior extraction", "actors generation", "questions generation"]
MODELS_DEFAULTS: Final[list[str]] = [DEFAULT_BEHAVIOR_EXTRACTION_MODEL, DEFAULT_ACTORS_GENERATION_MODEL, DEFAULT_QUESTIONS_GENERATION_MODEL]


class ActorsGenerationException(BaseAttackTechniqueHandlerException):
    """
    Exception for actors generation error.
    """
    ...


class BehaviorExtractionException(BaseAttackTechniqueHandlerException):
    """
    Exception for behavior extraction error.
    """
    ...


class QuestionsGenerationException(BaseAttackTechniqueHandlerException):
    """
    Exception for questions generation error.
    """
    ...


class AnswerGenerationException(BaseAttackTechniqueHandlerException):
    """
    Exception for answers generation error.
    """
    ...


class ActorAttackHandlerExtraParams(BaseModel):
    behavior_extraction_model: str = Field(DEFAULT_BEHAVIOR_EXTRACTION_MODEL,
                                           description=f"The model to extract the main behavior from the original prompt. default: {DEFAULT_BEHAVIOR_EXTRACTION_MODEL}")
    actors_generation_model: str = Field(DEFAULT_ACTORS_GENERATION_MODEL,
                                         description=f"The model to generate actors leading to the answer for the original prompt. default: {DEFAULT_ACTORS_GENERATION_MODEL}")
    questions_generation_model: str = Field(DEFAULT_QUESTIONS_GENERATION_MODEL,
                                            description=f"The model to generate questions using the actors and the behavior. default: {DEFAULT_QUESTIONS_GENERATION_MODEL}")


@attack_handler_fm.flavor(FuzzerAttackMode.ACTOR)
class ActorAttackHandler(BaseAttackTechniqueHandler[ActorAttackHandlerExtraParams]):
    """
    Actor attack handler - Inspired by actor-network theory, it builds semantic networks of "actors" to subtly guide
     conversations toward harmful targets while concealing malicious intent.
    """

    def __init__(self, **extra: Any):
        super().__init__(**extra)
        for index, model in enumerate([self._extra_args.behavior_extraction_model,
                                       self._extra_args.actors_generation_model,
                                       self._extra_args.questions_generation_model]):
            if model not in self._model_queue_map:
                raise RuntimeError(generate_model_error(MODELS_TYPES[index], model, MODELS_DEFAULTS[index]))

    async def _attack(self, prompt: str, **extra: Any) -> Optional[AttackResultEntry]:
        chat_messages: list[BaseLLMMessage] = []
        llm: BaseLLMProvider

        # Log the original prompt at the start
        self._log_iteration_step(
            iteration=0,
            prompt=prompt,
            response="",
            improvement="Starting actor attack - extracting behavior and generating actors..."
        )

        logger.info("Extracting main behavior from the prompt")
        async with self._borrow(self._extra_args.behavior_extraction_model) as llm:
            try:
                behavior_extraction_prompt = BEHAVIOR_EXTRACTION_PROMPT.format(ORIGINAL_PROMPT=prompt)
                behavior_extraction_response = await llm.generate(behavior_extraction_prompt)
                if behavior_extraction_response is None:
                    raise BehaviorExtractionException("No response from the behavior extraction model")
            except BaseLLMProviderException as e:
                raise BehaviorExtractionException(f"Problem occurred during behavior extraction. {e}")
        behavior_response = behavior_extraction_response.response

        logger.info("Generating relevant actors to the behavior")
        async with self._borrow(self._extra_args.actors_generation_model) as llm:
            try:
                actors_extraction_prompt = ACTORS_GENERATION_PROMPT.format(EXTRACTED_BEHAVIOR=behavior_response)
                actors_extraction_response = await llm.generate(actors_extraction_prompt)
                if actors_extraction_response is None:
                    raise ActorsGenerationException("No response from the actor generation model")
            except BaseLLMProviderException as e:
                raise ActorsGenerationException(f"Problem occurred during actor generation. {e}")
        actors_response = actors_extraction_response.response
        all_actors = actors_response.split(SPLIT_TOKEN)

        actors_questions_dict = {}

        logger.info("Generating questions based on the behavior and actors")
        async with self._borrow(self._extra_args.questions_generation_model) as llm:
            for actor in all_actors:
                try:
                    questions_generation_prompt = QUESTIONS_GENERATION_PROMPT.format(EXTRACTED_BEHAVIOR=behavior_response,
                                                                                     ACTOR=actor)
                    questions_generation_response = await llm.generate(questions_generation_prompt)
                    if questions_generation_response is None:
                        raise QuestionsGenerationException("No response from the questions generation model.")

                    single_actor_questions = questions_generation_response.response.split(SPLIT_TOKEN)
                    if single_actor_questions is None:
                        raise QuestionsGenerationException("No response from the questions generation model.")

                    if actor not in actors_questions_dict:
                        actors_questions_dict[actor] = single_actor_questions
                except BaseLLMProviderException as e:
                    raise QuestionsGenerationException(f"Problem occurred during questions generation. {e}")

        logger.info("Sending questions to the target model")
        classifications: dict[str, Any] = {}
        last_answer: Optional[BaseLLMProviderResponse] = None
        last_answer_response: str = ""
        last_actor: str = ""
        
        # Log actor attack conversation header
        _prompts_logger = base_module._prompts_logger
        if _prompts_logger is not None:
            attack_mode = self._attack_mode or "act"
            _prompts_logger.info(f"\n{'-'*80}")
            _prompts_logger.info(f"Attack Mode: {attack_mode.upper()} - Starting conversation with actors")
            _prompts_logger.info(f"{'-'*80}")
            # Force immediate flush
            for handler in _prompts_logger.handlers:
                handler.flush()
        
        # Process each actor: send questions, then classify (outside the borrow context to avoid deadlock)
        for actor_idx, actor in enumerate(actors_questions_dict, 1):
            logger.debug(f"Using actor {actor}")
            last_actor = actor
            current_prompt_str = "\n".join(actors_questions_dict[actor])
            
            # Log actor header (force flush)
            _prompts_logger = base_module._prompts_logger
            if _prompts_logger is not None:
                _prompts_logger.info(f"\nActor {actor_idx}: {actor}")
                _prompts_logger.info(f"{'-'*40}")
                # Force immediate flush
                for handler in _prompts_logger.handlers:
                    handler.flush()
            
            # Send questions for this actor (borrow target model)
            async with self._borrow(self._model) as llm:
                actor_chat_messages = chat_messages.copy()  # Copy the conversation so far
                for question in actors_questions_dict[actor]:
                    logger.debug(f"Running question {question}")
                    actor_chat_messages.append(BaseLLMMessage(role=LLMRole.USER, content=question))
                    
                    # Log the question immediately (force flush)
                    _prompts_logger = base_module._prompts_logger
                    if _prompts_logger is not None:
                        _prompts_logger.info(f"USER: {question}")
                        # Force immediate flush
                        for handler in _prompts_logger.handlers:
                            handler.flush()
                    
                    last_answer = await llm.chat(actor_chat_messages)
                    if last_answer is None:
                        raise AnswerGenerationException("No response from the target model")
                    last_answer_response = last_answer.response
                    
                    # Log the response immediately (force flush)
                    _prompts_logger = base_module._prompts_logger
                    if _prompts_logger is not None:
                        _prompts_logger.info(f"ASSISTANT: {last_answer_response}")
                        _prompts_logger.info("")  # Empty line for readability
                        # Force immediate flush
                        for handler in _prompts_logger.handlers:
                            handler.flush()
                    
                    actor_chat_messages.append(BaseLLMMessage(role=LLMRole.ASSISTANT, content=last_answer_response))
            
            # Release target model before classification to avoid deadlock
            # Now classify outside the target model borrowing context
            logger.debug(f"Classifying response for actor {actor}")
            try:
                classifications = await self._classify_llm_response(
                    last_answer, 
                    original_prompt=prompt,
                    current_prompt=current_prompt_str
                )
                logger.debug(f"Classification results: {classifications}")
            except Exception as e:
                logger.error(f"Error during classification: {e}", exc_info=True)
                classifications = {}
            
            # Log classification results (force flush)
            _prompts_logger = base_module._prompts_logger
            if _prompts_logger is not None:
                if classifications:
                    jailbreak_detected = any(x != 0 for x in classifications.values())
                    _prompts_logger.info(f"Classification: {classifications}")
                    _prompts_logger.info(f"Jailbreak detected: {'YES' if jailbreak_detected else 'NO'}")
                _prompts_logger.info(f"{'-'*40}\n")
                # Force immediate flush
                for handler in _prompts_logger.handlers:
                    handler.flush()
            
            # If jailbreak detected, we're done
            if classifications and any(x != 0 for x in classifications.values()):
                logger.info(f"Jailbreak detected for actor {actor}, breaking loop")
                # Log the jailbreak-triggering prompt and response
                _prompts_logger = base_module._prompts_logger
                if _prompts_logger is not None:
                    _prompts_logger.info(f"\n{'='*80}")
                    _prompts_logger.info("⚠️  JAILBREAK DETECTED - Triggering prompt/response pair:")
                    _prompts_logger.info(f"{'='*80}")
                    _prompts_logger.info(f"ACTOR: {actor}")
                    _prompts_logger.info(f"PROMPT: {current_prompt_str}")
                    _prompts_logger.info(f"RESPONSE: {last_answer_response}")
                    _prompts_logger.info(f"CLASSIFICATIONS: {classifications}")
                    _prompts_logger.info(f"{'='*80}\n")
                    # Force immediate flush
                    for handler in _prompts_logger.handlers:
                        handler.flush()
                break

        # Check if a jailbreak was actually detected
        jailbreak_detected = classifications and any(x != 0 for x in classifications.values())
        
        # Log final summary - show whether jailbreak was detected or not
        _prompts_logger = base_module._prompts_logger
        if _prompts_logger is not None:
            if jailbreak_detected:
                _prompts_logger.info("✓ JAILBREAK SUCCESSFUL - Attack completed with jailbreak detected")
            else:
                _prompts_logger.info("✗ NO JAILBREAK - Attack completed but no jailbreak was detected")
            _prompts_logger.info(f"{'='*80}\n")
            # Force immediate flush
            for handler in _prompts_logger.handlers:
                handler.flush()

        result = AttackResultEntry(original_prompt=prompt,
                                   current_prompt="\n".join(actors_questions_dict[last_actor]) if last_actor else prompt,
                                   response=last_answer_response) if last_answer else None
        logger.debug("Response: %s", last_answer_response if last_answer else "None")

        if result:
            result.classifications = classifications

        return result

    @classmethod
    def extra_args_cls(cls) -> Type[BaseModel]:
        return ActorAttackHandlerExtraParams
