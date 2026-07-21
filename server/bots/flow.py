"""Flow-based bot implementation using the base bot framework (Pipecat 1.x flows).

Migrated to ``pipecat.flows`` (bundled in pipecat-ai 1.5.0):

- Node dicts are ``NodeConfig``; functions are ``FlowsFunctionSchema`` objects.
- The 0.x split of ``handler`` (collect) + ``transition_callback`` (route) is merged
  into a single *consolidated* handler ``(args, flow_manager) -> (result, next_node)``.
- ``FlowManager`` takes ``worker=`` (no ``tts=``); transitions use the returned node
  (or ``set_node_from_config``) instead of the removed ``set_node(id, config)``.
- The web client / RTVI navigation is retired (the phone/Daily path has no browser),
  so ``execute_navigation`` is a logged no-op kept for structural compatibility.
"""

from typing import Dict, Optional, Tuple

from dotenv import load_dotenv
from loguru import logger

from pipecat.flows import (
    FlowManager,
    NodeConfig,
    FlowsFunctionSchema,
    ContextStrategyConfig,
)
from pipecat.flows.types import ContextStrategy

from bots.base_bot import BaseBot
from config.bot import BotConfig
from prompts import (
    get_recording_consent_prompt,
    get_name_and_interest_prompt,
    get_development_prompt,
    get_close_call_prompt,
)

# Load environment variables from .env file
load_dotenv()

# Logging is configured by the entrypoint (main.py for the WebSocket/FreeSWITCH path,
# runner.py for the Daily subprocess); this module does not reconfigure loguru sinks.


# ==============================================================================
# Node Configurations
# ==============================================================================


def create_recording_consent_node() -> NodeConfig:
    """# Node 1: Recording Consent Node
    Create initial node that requests recording consent."""
    return {
        **get_recording_consent_prompt(),
        "name": "recording_consent",
        "functions": [
            FlowsFunctionSchema(
                name="collect_recording_consent",
                description="Record whether the user consents to the call being recorded",
                properties={
                    "recording_consent": {
                        "type": "boolean",
                        "description": "True if the user consents to being recorded, False otherwise",
                    }
                },
                required=["recording_consent"],
                handler=collect_recording_consent,
            )
        ],
    }


def create_name_and_interest_node() -> NodeConfig:
    """# Node 2: Collect Name and Interest Node
    Create node that collects user's name and primary interest."""
    return {
        **get_name_and_interest_prompt(),
        "name": "name_and_interest",
        "functions": [
            FlowsFunctionSchema(
                name="collect_name_and_interest",
                description="Collect user's name and primary interest",
                properties={
                    "name": {"type": "string"},
                    "interest_type": {
                        "type": "string",
                        "enum": [
                            "technical_consultation",
                            "voice_agent_development",
                        ],
                    },
                },
                required=["name", "interest_type"],
                handler=collect_name_and_interest,
            )
        ],
    }


def create_development_node(user_name: str = None) -> NodeConfig:
    """# Node 3: Development Node
    Create node for handling voice agent development path."""
    return {
        **get_development_prompt(user_name),
        "name": "development",
        "functions": [
            FlowsFunctionSchema(
                name="collect_qualification_data",
                description="Collect qualification information",
                properties={
                    "use_case": {"type": "string"},
                    "timeline": {"type": "string"},
                    "budget": {"type": "integer"},
                    "feedback": {"type": "string"},
                },
                required=["use_case", "timeline", "budget", "feedback"],
                handler=collect_qualification_data,
            )
        ],
    }


def create_close_call_node(user_name: str = None) -> NodeConfig:
    """# Node 4: Final Close Node
    Create node to conclude the conversation."""
    return {
        **get_close_call_prompt(user_name),
        "name": "close_call",
        "functions": [],
        "post_actions": [{"type": "end_conversation"}],
    }


# ==============================================================================
# Consolidated Function Handlers  (args, flow_manager) -> (result, next_node)
# ==============================================================================


async def collect_recording_consent(
    args: dict, flow_manager: FlowManager
) -> Tuple[dict, Optional[NodeConfig]]:
    """Process recording consent and route to the next node."""
    consent = args["recording_consent"]
    flow_manager.state["recording_consent"] = consent

    if consent:
        return {"recording_consent": True}, create_name_and_interest_node()

    # No consent: go directly to close call with contact-form navigation.
    close_node = create_close_call_node()
    close_node["pre_actions"] = [
        {
            "type": "tts_say",
            "text": "For now I've navigated you to our contact form where you can send us your questions or requirements in writing. Feel free to call back if you change your mind.",
        },
        {"type": "execute_navigation", "path": "/contact"},
    ]
    return {"recording_consent": False}, close_node


async def collect_name_and_interest(
    args: dict, flow_manager: FlowManager
) -> Tuple[dict, Optional[NodeConfig]]:
    """Collect the user's name and interest, then route."""
    name = args.get("name")
    interest_type = args["interest_type"]
    flow_manager.state["name"] = name
    flow_manager.state["interest_type"] = interest_type

    result = {"name": name, "interest_type": interest_type}

    if interest_type == "technical_consultation":
        close_call = add_consultancy_pre_actions(create_close_call_node(name))
        return result, close_call
    elif interest_type == "voice_agent_development":
        return result, create_development_node(name)

    # Fallback (should not happen given the enum) -> re-collect.
    return result, None


async def collect_qualification_data(
    args: dict, flow_manager: FlowManager
) -> Tuple[dict, Optional[NodeConfig]]:
    """Process qualification data, decide qualified/not, and route to close."""
    result = {
        "use_case": args["use_case"],
        "timeline": args["timeline"],
        "budget": args["budget"],
        "feedback": args["feedback"],
    }
    flow_manager.state.update(result)

    qualified = (
        bool(args.get("use_case"))
        and bool(args.get("timeline"))
        and args.get("budget", 0) >= 1000
        and bool(args.get("feedback"))
    )

    logger.debug(f"Qualified: {qualified} based on: {args}")

    name = flow_manager.state.get("name")
    close_call = add_development_pre_actions(create_close_call_node(name), qualified)
    return result, close_call


# ==============================================================================
# Navigation Handling  (web-client actions; inert on the phone/Daily path)
# ==============================================================================


def add_consultancy_pre_actions(node: NodeConfig) -> NodeConfig:
    """Add pre-actions for consultancy navigation."""
    node["pre_actions"] = [
        {
            "type": "tts_say",
            "text": "I've navigated you to our consultancy booking page where you can set up a video conference with our founder to discuss your needs in more detail. Please provide as much detail as you can when you book, to assist us in preparing for the call.",
        },
        {"type": "execute_navigation", "path": "/consultancy"},
    ]
    return node


def add_development_pre_actions(node: NodeConfig, qualified: bool) -> NodeConfig:
    """Add pre-actions for development navigation."""
    nav_message = (
        "I've navigated you to our discovery call booking page where you can schedule a free discovery call to discuss your requirements in more detail."
        if qualified
        else "I've navigated you to our contact form where you can send us more details about your requirements."
    )

    node["pre_actions"] = [
        {"type": "tts_say", "text": nav_message},
        {
            "type": "execute_navigation",
            "path": "/discovery" if qualified else "/contact",
        },
    ]
    return node


# ==============================================================================
# Bot Implementation
# ==============================================================================


class FlowBot(BaseBot):
    """Flow-based bot implementation (Pipecat 1.x flows)."""

    def __init__(self, config: BotConfig):
        super().__init__(config)

        # Initialize flow-specific components
        self.flow_manager: Optional[FlowManager] = None

    async def _handle_first_participant(self):
        """Handle first participant by initializing the flow manager."""
        self.flow_manager = FlowManager(
            worker=self.worker,
            llm=self.llm,
            context_aggregator=self.context_aggregator,
            context_strategy=ContextStrategyConfig(strategy=ContextStrategy.RESET),
        )

        # Register navigation action (inert on the phone path; kept for structure).
        self.flow_manager.register_action(
            "execute_navigation", self._handle_navigation_action
        )

        # Initialize the flow at the recording-consent node.
        await self.flow_manager.initialize(create_recording_consent_node())

    async def _handle_navigation_action(self, action: dict, flow_manager: FlowManager):
        """Log the requested navigation. The web client is retired, so this is a no-op."""
        path = action.get("path")
        logger.debug(f"execute_navigation requested (no web client): path={path}")
