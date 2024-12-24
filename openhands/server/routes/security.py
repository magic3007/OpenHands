from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)

from openhands.security import SecurityAnalyzer, options
from openhands.server.shared import config

app = APIRouter(prefix='/api/conversations/{conversation_id}')


@app.route('/security/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
async def security_api(request: Request):
    """Catch-all route for security analyzer API requests.

    Each request is handled directly to the security analyzer.

    Args:
        request (Request): The incoming FastAPI request object.

    Returns:
        Any: The response from the security analyzer.

    Raises:
        HTTPException: If the security analyzer is not initialized.
    """
    if not request.state.runtime:
        raise HTTPException(status_code=404, detail='Security analyzer not initialized')
    security_analyzer = options.SecurityAnalyzers.get(
        config.security.security_analyzer or '', SecurityAnalyzer
    )(request.state.runtime.event_stream)

    return await security_analyzer.handle_api_request(request)
