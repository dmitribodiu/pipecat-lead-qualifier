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
from services.config_api import config_api


class MultiAgentPaymentBot(BaseBot):
    """Router + inquiry + per-bill collection + shared settlement, one FlowManager."""

    def __init__(self, config):
        super().__init__(config)
        self.flow_manager: Optional[FlowManager] = None
        # Dialed number for this call, set by the transport entrypoint (main.py) from the
        # fork URL. Used to resolve which tenant the call belongs to; None => fallback.
        self.call_did: Optional[str] = None

    async def _handle_first_participant(self):
        """Start the call at the Concierge greeting node.

        Configuration injection point: resolve the tenant from the dialed number and load
        its config BEFORE the first node is built, then seed it into ``flow_manager.state``
        so every node reads its wording/limits from there. One config read per call.
        """
        tenant_cfg = await config_api.load_for_call(self.call_did)

        self.flow_manager = FlowManager(
            worker=self.worker,
            llm=self.llm,
            context_aggregator=self.context_aggregator,
            # FAQ is global — answerable from any node without a handoff.
            global_functions=[get_business_info],
        )
        # Seed the tenant config into per-call state (NOT a module global — bots run
        # in-process, one per call). Everything the flow says/gates on flows from here.
        self.flow_manager.state["tenant"] = tenant_cfg.settings
        self.flow_manager.state["tenant_id"] = tenant_cfg.tenant_id

        # Keep Perfetto tracing (TRACE_CALLS=1): dumps a context snapshot per node change,
        # which now doubles as an agent-handoff trace.
        self.trace_flow_nodes(self.flow_manager)
        await self.flow_manager.initialize(build_greeting_node(self.flow_manager.state))
