import asyncio
import sys
import os
import json
import logging
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from typing import AsyncGenerator, Optional, Dict, Any, Callable, Union

# Set up proper logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("agent_service")

load_dotenv()  # Load environment variables from .env

class AgentService:
    """Core service layer for agent interactions that can be used by multiple frontends."""
    
    def __init__(self, system_prompt: str = "You are a helpful assistant. Only use tools if needed for a user query."):
        model_name = os.getenv("BASE_MODEL")
        if not model_name:
            logger.error("BASE_MODEL environment variable is not set")
            raise ValueError("BASE_MODEL environment variable is required")
            
        logger.info(f"Initializing agent with model: {model_name}")
        
        self.agent = Agent(
            model_name,            
            mcp_servers=[MCPServerStdio(sys.executable, args=["mcp_server.py"])],
            system_prompt=system_prompt,
        )
        
    async def __aenter__(self):
        """Start MCP servers when used as context manager."""
        self._mcp_context = self.agent.run_mcp_servers()
        await self._mcp_context.__aenter__()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Stop MCP servers when exiting context."""
        await self._mcp_context.__aexit__(exc_type, exc_val, exc_tb)
    
    async def process_input(self, 
                           user_input: str, 
                           history: Optional[list] = None,
                           on_assistant_message: Optional[Callable] = None,
                           on_tool_call: Optional[Callable] = None,
                           on_tool_result: Optional[Callable] = None) -> Dict[str, Any]:
        """
        Process user input and handle the agent response.
        
        Args:
            user_input: The text input from the user
            history: Optional conversation history
            on_assistant_message: Callback for streaming assistant responses
            on_tool_call: Callback for tool calls
            on_tool_result: Callback for tool results
            
        Returns:
            Dict containing assistant response, tool calls and results
        """
        response = {
            "assistant_content": "",
            "tool_calls": [],
            "tool_results": []
        }
        
        try:
            # Format history in a cleaner way if provided
            input_with_context = self._prepare_input_with_history(user_input, history)
            logger.info(f"Processing input: {user_input[:50]}...")
            
            async with self.agent.iter(input_with_context) as run:
                await self._process_agent_run(run, response, on_assistant_message, on_tool_call, on_tool_result)
                
        except Exception as e:
            logger.exception(f"Error processing input: {e}")
            response["error"] = str(e)
            
        return response

    def _prepare_input_with_history(self, user_input: str, history: Optional[list]) -> str:
        """Prepare the input with context from conversation history if available."""
        if not history:
            return user_input
            
        # Consider using a more structured approach than just dumping JSON
        return f"Previous conversation:\n{json.dumps(history, indent=2)}\n\nCurrent query: {user_input}"
    
    async def _process_agent_run(self, run, response: Dict[str, Any],
                                on_assistant_message: Optional[Callable],
                                on_tool_call: Optional[Callable],
                                on_tool_result: Optional[Callable]):
        """Process the agent run and update the response dictionary."""
        async for node in run:
            if Agent.is_model_request_node(node):
                await self._handle_model_request(node, run.ctx, response, on_assistant_message)
                    
            elif Agent.is_call_tools_node(node):
                await self._handle_tool_call(node, run.ctx, response, on_tool_call, on_tool_result)
    
    async def _handle_model_request(self, node, ctx, response, on_assistant_message):
        """Process model generation events."""
        async with node.stream(ctx) as stream:
            collected_content = ""
            async for event in stream:
                logger.debug(f"Model event: {type(event)}")
                if hasattr(event, 'delta') and hasattr(event.delta, 'content_delta'):
                    delta = event.delta.content_delta
                    if delta:
                        collected_content += delta
                        if on_assistant_message:
                            on_assistant_message(delta)
            
            response["assistant_content"] = collected_content
    
    async def _handle_tool_call(self, node, ctx, response, on_tool_call, on_tool_result):
        """Process tool call events."""
        async with node.stream(ctx) as stream:
            async for event in stream:
                logger.debug(f"Tool event: {type(event)}")
                # Handle tool call
                if hasattr(event, 'part') and hasattr(event.part, 'tool_name'):
                    if event.part.args and event.part.args.strip() not in ["", "{}"]:
                        tool_call = {
                            "tool_name": event.part.tool_name,
                            "args": event.part.args
                        }
                        response["tool_calls"].append(tool_call)
                        if on_tool_call:
                            on_tool_call(tool_call)
                
                # Handle tool result
                elif hasattr(event, 'result') and hasattr(event.result, 'content'):
                    result = event.result.content.content[0].text
                    response["tool_results"].append(result)
                    if on_tool_result:
                        on_tool_result(result)