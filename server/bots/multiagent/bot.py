"""MultiAgentPaymentBot — entry point wiring the sub-flow agents into one FlowManager.

Same pipeline/transport as PaymentBot; the only difference is the flow starts at the
Concierge and the nodes are organised into agent groups across this package.

To run it, wire a bot type in main.py's `/audio` handler (leaving payment.py alone):

    elif config.bot_type == "multiagent":
        from bots.multiagent.bot import MultiAgentPaymentBot
        bot = MultiAgentPaymentBot(config)

and add "multiagent" to the --bot-type choices. Then BOT_TYPE=multiagent.
"""

from typing import Optional

from pipecat.flows import FlowManager

from bots.base_bot import BaseBot
from bots.multiagent.faq import get_business_info
from bots.multiagent.concierge import build_greeting_node


class MultiAgentPaymentBot(BaseBot):
    """Router + inquiry + per-bill collection + shared settlement, one FlowManager."""

    def __init__(self, config):
        super().__init__(config)
        self.flow_manager: Optional[FlowManager] = None

    async def _handle_first_participant(self):
        """Start the call at the Concierge greeting node."""
        self.flow_manager = FlowManager(
            worker=self.worker,
            llm=self.llm,
            context_aggregator=self.context_aggregator,
            # FAQ is global — answerable from any node without a handoff.
            global_functions=[get_business_info],
        )
        # Keep Perfetto tracing (TRACE_CALLS=1): dumps a context snapshot per node change,
        # which now doubles as an agent-handoff trace.
        self.trace_flow_nodes(self.flow_manager)
        await self.flow_manager.initialize(build_greeting_node())
