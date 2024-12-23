import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from github import Github
from pydantic import BaseModel

from openhands.core.logger import openhands_logger as logger
from openhands.server.routes.settings import SettingsStoreImpl
from openhands.server.session.conversation_init_data import ConversationInitData
from openhands.server.shared import config, session_manager
from openhands.storage.conversation.conversation_store import (
    ConversationMetadata,
    ConversationStore,
)
from openhands.utils.async_utils import call_sync_from_async

app = APIRouter(prefix='/api')


class InitSessionRequest(BaseModel):
    github_token: str | None = None
    latest_event_id: int = -1
    selected_repository: str | None = None
    args: dict | None = None


@app.post('/conversations')
async def new_conversation(request: Request, data: InitSessionRequest):
    """Initialize a new session or join an existing one.
    After successful initialization, the client should connect to the WebSocket
    using the returned conversation ID
    """
    github_token = ''
    if data.github_token:
        github_token = data.github_token

    settings_store = await SettingsStoreImpl.get_instance(config, github_token)
    settings = await settings_store.load()

    session_init_args: dict = {}
    if settings:
        session_init_args = {**settings.__dict__, **session_init_args}
    if data.args:
        for key, value in data.args.items():
            session_init_args[key.lower()] = value

    session_init_args['github_token'] = github_token
    session_init_args['selected_repository'] = data.selected_repository
    conversation_init_data = ConversationInitData(**session_init_args)

    conversation_store = await ConversationStore.get_instance(config)

    conversation_id = uuid.uuid4().hex
    while await conversation_store.exists(conversation_id):
        logger.warning(f'Collision on conversation ID: {conversation_id}. Retrying...')
        conversation_id = uuid.uuid4().hex

    user_id = ''
    if data.github_token:
        g = Github(data.github_token)
        gh_user = await call_sync_from_async(g.get_user)
        user_id = gh_user.id

    await conversation_store.save_metadata(
        ConversationMetadata(
            conversation_id=conversation_id,
            github_user_id=user_id,
            selected_repository=data.selected_repository,
        )
    )

    await session_manager.maybe_start_agent_loop(
        conversation_id, conversation_init_data
    )
    return JSONResponse(content={'status': 'ok', 'conversation_id': conversation_id})


@app.get('/conversations')
async def search_conversations(request: Request):
    file_store = session_manager.file_store
    conversations = []
    try:
        for path in file_store.list('sessions/') or []:
            try:
                session_id = path.split('/')[1]
                if not session_id.startswith('.'):
                    events = file_store.list(f'{path}events/')
                    events = sorted(events)
                    event_path = events[-1]
                    event = json.loads(file_store.read(event_path))
                    conversations.append(
                        {
                            'id': session_id,
                            'last_accessed_at': event.get('timestamp'),
                            'status': 'RUNNING'
                            if session_manager.is_agent_loop_running(session_id)
                            else 'STOPPED',
                        }
                    )
            except Exception:  # type: ignore
                logger.warning(
                    f'Error loading path: {path}', exc_info=True, stack_info=True
                )
    except Exception:  # type: ignore
        logger.warning('Error loading conversation', exc_info=True, stack_info=True)
    return {
        'conversations': conversations,
        'has_more': False,
    }
