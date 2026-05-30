from livekit.agents import RunContext


def acknowledge(context: RunContext) -> None:
    try:
        context.session.say("One moment, sir.", allow_interruptions=True)
    except Exception:
        pass
