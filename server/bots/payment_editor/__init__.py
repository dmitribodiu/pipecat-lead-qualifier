"""Editor-shaped copy of the payment bot.

Same behaviour as bots/payment.py, restructured so the *routing* is controllable from
the Pipecat Flows visual editor: named parameterless node factories, one discrete
``result`` per routing handler, and a single if/elif decision block per handler (the
shape the editor generates and re-imports). See README.md for the mapping.
"""
