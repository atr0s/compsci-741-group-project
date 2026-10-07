"""
Web Interface for Azure AI Agent Chat

This web application provides a user interface for interacting with the AI agent
through REST API calls to the agent service.

To run: python web_app.py
Web interface available at: http://127.0.0.1:8005
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional

# Add workshop folder to path to import shared modules
sys.path.append(str(Path(__file__).parent.parent / "workshop"))

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from foundry_backend import BasicAuthMiddleware, FoundryAgentBackend, load_users

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
load_dotenv(override=False)

# Configure logging
logging.basicConfig(level=logging.ERROR)

# Agent service configuration.
#
# AGENT_BACKEND selects where a turn is executed:
#   foundry  Foundry Agent Service runs the tool loop in Azure and calls the MCP
#            server outbound. Nothing else needs hosting.
#   proxy    Forward to a local agent_service.py. The original behaviour, kept
#            as the offline fallback.
AGENT_BACKEND = os.getenv("AGENT_BACKEND", "proxy").strip().lower()
AGENT_SERVICE_URL = os.getenv("AGENT_SERVICE_URL", "http://127.0.0.1:8006")


class WebApp:
    """Handles all web interface functionality for the AI Agent Chat application."""
    
    def __init__(self, app: FastAPI) -> None:
        """Initialize the web interface with FastAPI app."""
        self.app = app
        self.chat_sessions: Dict[str, List[Dict]] = {}
        self.backend = FoundryAgentBackend() if AGENT_BACKEND == "foundry" else None

        self._setup_routes()
        self._setup_static_files()
    
    def _setup_static_files(self) -> None:
        """Setup static file serving."""
        static_dir = Path(__file__).parent.parent.parent / "shared" / "static"
        self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    
    def _setup_routes(self) -> None:
        """Setup all web routes."""
        self.app.get("/", response_class=HTMLResponse)(self.get_chat_page)
        self.app.get("/favicon.ico", response_class=FileResponse)(self.get_favicon)
        self.app.post("/upload")(self.upload_file)
        self.app.get("/chat/stream")(self.stream_chat)
        self.app.get("/files/{filename}")(self.serve_file)
        self.app.get("/health")(self.health_check)
        self.app.get("/stores")(self.list_stores)
        self.app.get("/agents")(self.list_agents)
    
    async def get_chat_page(self) -> HTMLResponse:
        """Serve the chat HTML page."""
        html_file = Path(__file__).parent.parent.parent / "shared" / "static" / "index.html"
        with html_file.open("r") as f:
            return HTMLResponse(content=f.read())
    
    async def get_favicon(self) -> FileResponse:
        """Serve the favicon.ico file."""
        favicon_path = Path(__file__).parent.parent.parent / "shared" / "static" / "favicon.ico"
        return FileResponse(favicon_path, media_type="image/x-icon")
    
    async def upload_file(self, file: UploadFile, message: str = Form(None)) -> Dict:
        """Handle file upload and extract text content."""
        try:
            # Check file size (10MB limit)
            content = await file.read()
            if len(content) > 10 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="File too large (max 10MB)")

            # Extract text based on file type
            file_text = ""
            file_extension = (
                file.filename.lower().split(".")[-1] if file.filename and "." in file.filename else ""
            )

            if file_extension in ["txt", "md"]:
                file_text = content.decode("utf-8")
            elif file_extension in ["pdf"]:
                file_text = f"[PDF file: {file.filename}]"  # Could integrate PDF parsing
            elif file_extension in ["doc", "docx"]:
                file_text = f"[Word document: {file.filename}]"  # Could integrate Word parsing
            else:
                file_text = f"[Uploaded file: {file.filename}]"

            # Prepare the message with file content
            if message:
                combined_message = f"{message}\n\nAttached file content:\n{file_text}"
            else:
                combined_message = f"Please analyze this file:\n\n{file_text}"

            return {"content": combined_message, "filename": file.filename}

        except Exception as e:
            return {"error": f"Error processing file: {e!s}"}
    
    def _store_for(self, request: Request) -> Optional[str]:
        """Which store this request may use.

        The submitted value is a CHOICE WITHIN an entitlement, never the
        entitlement itself: a user holding one store gets it regardless of what
        the URL says, and a user asking for a store they do not hold is refused
        rather than quietly redirected - a forged store is a bug or an attack,
        and both deserve an error.
        """
        user = request.state.user
        requested = (request.query_params.get("store") or "").strip().lower()
        if requested and requested not in user.stores:
            return None
        return requested or user.stores[0]

    async def stream_chat(self, request: Request, message: str = "") -> Response:
        """Stream chat responses by proxying to the agent service."""
        if not message.strip():
            return StreamingResponse(
                iter([f"data: {json.dumps({'error': 'Empty message'})}\n\n"]),
                media_type="text/event-stream",
            )

        store = self._store_for(request)
        if store is None:
            logging.warning("store not permitted for user %s", request.state.user.name)
            return JSONResponse({"error": "Store not permitted"}, status_code=403)

        agent_key = (request.query_params.get("agent") or "").strip().lower()

        # Keyed by the signed-in user. A shared "default" session would put every
        # manager on one Foundry conversation, so Kirkland would read Seattle's history.
        session_id = request.state.user.name
        if session_id not in self.chat_sessions:
            self.chat_sessions[session_id] = []

        # Add user message to session
        self.chat_sessions[session_id].append({"role": "user", "content": message})

        return StreamingResponse(
            self._generate_stream(message, session_id, store, agent_key),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", 
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
                "Content-Encoding": "identity"
            },
        )
    
    async def _generate_stream(self, message: str, session_id: str, store: str = "",
                               agent_key: str = "") -> AsyncGenerator[str, None]:
        """Stream a turn, either from Foundry or by proxying to agent_service.py."""
        if self.backend is not None:
            async for chunk in self.backend.stream(message, session_id, store, agent_key):
                yield chunk
            return

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                # Make request to agent service
                request_data = {
                    "message": message,
                    "session_id": session_id
                }
                
                async with client.stream(
                    "POST",
                    f"{AGENT_SERVICE_URL}/chat/stream",
                    json=request_data,
                    headers={"Accept": "text/event-stream"}
                ) as response:
                    if response.status_code != 200:
                        yield f"data: {json.dumps({'error': f'Agent service error: {response.status_code}'})}\n\n"
                        return
                    
                    assistant_message = ""
                    async for chunk in response.aiter_text():
                        if chunk.strip():
                            # Parse and forward each chunk
                            lines = chunk.strip().split('\n')
                            for line in lines:
                                if line.startswith('data: '):
                                    data_str = line[6:]  # Remove 'data: ' prefix
                                    try:
                                        data = json.loads(data_str)
                                        
                                        # Convert agent service response format to web format
                                        if data.get("content"):
                                            assistant_message += data["content"]
                                            yield f"data: {json.dumps({'content': data['content']})}\n\n"
                                        elif data.get("file_info"):
                                            yield f"data: {json.dumps({'file': data['file_info']})}\n\n"
                                        elif data.get("error"):
                                            yield f"data: {json.dumps({'error': data['error']})}\n\n"
                                        elif data.get("done"):
                                            # Agent service signals completion
                                            break
                                    except json.JSONDecodeError:
                                        # Skip malformed JSON
                                        continue
                    
                    # Add complete message to session
                    if assistant_message:
                        self.chat_sessions[session_id].append({
                            "role": "assistant", 
                            "content": assistant_message
                        })

            # Send completion signal
            yield "data: [DONE]\n\n"

        except httpx.RequestError as e:
            yield f"data: {json.dumps({'error': f'Connection error to agent service: {e!s}'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'Streaming error: {e!s}'})}\n\n"
    
    async def serve_file(self, filename: str) -> FileResponse:
        """Proxy file serving to agent service or serve locally."""
        try:
            # Try to proxy to agent service first
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{AGENT_SERVICE_URL}/files/{filename}")
                if response.status_code == 200:
                    # Save file temporarily and serve it
                    temp_file = Path("/tmp") / filename
                    with temp_file.open("wb") as f:
                        f.write(response.content)
                    return FileResponse(path=str(temp_file))
        except Exception:
            pass
        
        # Fallback to local file serving
        files_dir = Path() / "images"
        file_path = files_dir / filename
        
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        
        # Security check: ensure the file is within the files directory
        try:
            file_path.resolve().relative_to(files_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Access denied") from exc
        
        return FileResponse(path=str(file_path))
    
    async def list_stores(self, request: Request) -> Dict:
        """Stores THIS user may choose between.

        Labels and keys only - never the RLS UUIDs, which stay server-side so
        the browser cannot name a tenant directly. Returning only the caller's
        own entitlement means the picker cannot offer a store the server would
        refuse anyway.
        """
        from stores import label_for

        user = request.state.user
        return {
            "stores": [{"key": k, "label": label_for(k)} for k in user.stores],
            "default": user.stores[0],
            "locked": len(user.stores) == 1,
            "user": user.name,
        }

    async def list_agents(self, request: Request) -> Dict:
        """Agents this deployment can actually run.

        Only provisioned agents are listed, so the picker cannot put the user on
        a run that would fail at create time. `all_stores` tells the UI whether
        the store picker is meaningful for that agent: for the customer-facing
        advisor the store is settled in conversation, not chosen up front.
        """
        import agents as agent_registry

        return {
            "agents": [
                {"key": a.key, "label": a.label, "blurb": a.blurb, "all_stores": a.all_stores}
                for a in agent_registry.available()
            ],
            "default": agent_registry.DEFAULT_AGENT_KEY,
        }

    async def health_check(self) -> Dict:
        """Check health of web app and agent service."""
        web_status = {"status": "healthy", "service": "web_interface", "backend": AGENT_BACKEND}

        if self.backend is not None:
            import agents as agent_registry

            return {
                **web_status,
                "agents": {a.key: agent_registry.agent_name_for(a) for a in agent_registry.available()},
            }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{AGENT_SERVICE_URL}/health")
                if response.status_code == 200:
                    agent_status = response.json()
                    return {
                        **web_status,
                        "agent_service": agent_status
                    }
                return {
                    **web_status,
                    "agent_service": {"status": "error", "code": response.status_code}
                }
        except Exception as e:
            return {
                **web_status,
                "agent_service": {"status": "error", "error": str(e)}
            }


# FastAPI app
app = FastAPI(title="Azure AI Agent Web Interface")

# Gate the whole app, unconditionally. This used to be conditional, which meant
# a missing app setting silently published the retail database to the internet.
# load_users() now raises instead, so a misconfiguration fails closed and loud.
app.add_middleware(BasicAuthMiddleware, users=load_users())

# Initialize web app
web_app = WebApp(app)


if __name__ == "__main__":
    import uvicorn

    # PORT comes first because App Service assigns it; 127.0.0.1 would be
    # unreachable from the platform front end.
    host = os.getenv("WEB_APP_HOST", "0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("WEB_APP_PORT") or 8005)

    print(f"Starting web interface on {host}:{port} (backend={AGENT_BACKEND})")
    uvicorn.run(app, host=host, port=port)
