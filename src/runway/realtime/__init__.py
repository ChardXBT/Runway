"""Real-time platform hooks (spec 04): signed webhook intake and processing."""

# The job kind shared by the intake routes (producer) and the worker handler
# (consumer); defined here so the route module never imports the handler.
PROCESS_WEBHOOK_JOB = "process_webhook"
