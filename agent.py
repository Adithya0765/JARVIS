import asyncio

from dotenv import load_dotenv

from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent, room_io, ChatMessage
from livekit.plugins import (
    google,
    ai_coustics,
)

from app_management import APP_TOOLS
from file_operations import FILE_TOOLS
from memory import MemoryService
from system_control import SYSTEM_TOOLS, run_system_monitor

load_dotenv(".env.local")

memory = MemoryService()


class Assistant(Agent):
    BASE_INSTRUCTIONS = (
        "You are JARVIS — Just A Rather Very Intelligent System. "
        "Formal, precise, British cadence. Address user as 'sir'. "
        "No contractions. Complete sentences. Dry wit delivered straight. "
        "Analytical, proactive, efficient. Acknowledge commands before calling tools."
    )

    def __init__(self) -> None:
        super().__init__(
            instructions=self.BASE_INSTRUCTIONS,
            tools=[*SYSTEM_TOOLS, *FILE_TOOLS, *APP_TOOLS],
        )
        self._session_id: str = ""
        self._monitor_task: asyncio.Task | None = None

    async def on_enter(self) -> None:
        self._session_id = memory.mark_session_start()
        context = memory.build_memory_context()
        if context:
            asyncio.create_task(self.update_instructions(f"{self.BASE_INSTRUCTIONS}\n\n{context}"))
        self._monitor_task = asyncio.create_task(
            run_system_monitor(self.session, self._session_id)
        )

    async def on_exit(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()


server = AgentServer()


@server.rtc_session(agent_name="my-agent")
async def my_agent(ctx: agents.JobContext):
    if not memory._running:
        await memory.start()

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            voice="Charon",
            temperature=0.7,
            top_k=20,
        )
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(model=ai_coustics.EnhancerModel.QUAIL_VF_S),
            ),
        ),
    )

    agent_instance = session.current_agent
    if isinstance(agent_instance, Assistant):
        @session.on("conversation_item_added")
        def on_conversation_item(event):
            item = event.item
            if isinstance(item, ChatMessage) and item.content:
                content = item.text_content if hasattr(item, 'text_content') else str(item.content)
                memory.log_turn(agent_instance._session_id, item.role, content)

        @session.on("close")
        def on_close(_event):
            memory.mark_session_end(agent_instance._session_id)

    await session.generate_reply(
        instructions="Greet the user and await their first command, sir."
    )


if __name__ == "__main__":
    agents.cli.run_app(server)