"""
Report Agent service
Uses LangChain + Zep to generate simulation reports with the ReACT pattern.

Capabilities:
1. Generate a report from the simulation requirement and the Zep knowledge graph
2. First plan the outline structure, then generate it section by section
3. Each section uses a multi-round ReACT reasoning-and-reflection loop
4. Supports chatting with the user, autonomously calling retrieval tools mid-conversation
"""

import os
import json
import time
import re
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..config import Config
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from ..utils.paths import safe_join
from .zep_tools import (
    ZepToolsService, 
    SearchResult, 
    InsightForgeResult, 
    PanoramaResult,
    InterviewResult
)

logger = get_logger('mirofish.report_agent')


class ReportLogger:
    """
    Report Agent detailed logger

    Writes an agent_log.jsonl file inside the report folder, recording every
    detailed step. Each line is a complete JSON object containing the timestamp,
    action type, detailed content, etc.
    """

    def __init__(self, report_id: str):
        """
        Initialize the logger

        Args:
            report_id: report ID, used to determine the log file path
        """
        self.report_id = report_id
        self.log_file_path = safe_join(
            Config.UPLOAD_FOLDER, 'reports', report_id, 'agent_log.jsonl'
        )
        self.start_time = datetime.now()
        self._ensure_log_file()

    def _ensure_log_file(self):
        """Ensure the directory containing the log file exists"""
        log_dir = os.path.dirname(self.log_file_path)
        os.makedirs(log_dir, exist_ok=True)

    def _get_elapsed_time(self) -> float:
        """Get the elapsed time from start until now (seconds)"""
        return (datetime.now() - self.start_time).total_seconds()
    
    def log(
        self, 
        action: str, 
        stage: str,
        details: Dict[str, Any],
        section_title: str = None,
        section_index: int = None
    ):
        """
        Record a single log entry

        Args:
            action: action type, e.g. 'start', 'tool_call', 'llm_response', 'section_complete'
            stage: current stage, e.g. 'planning', 'generating', 'completed'
            details: dict of detailed content, not truncated
            section_title: current section title (optional)
            section_index: current section index (optional)
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": round(self._get_elapsed_time(), 2),
            "report_id": self.report_id,
            "action": action,
            "stage": stage,
            "section_title": section_title,
            "section_index": section_index,
            "details": details
        }
        
        # Append to the JSONL file
        with open(self.log_file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

    def log_start(self, simulation_id: str, graph_id: str, simulation_requirement: str):
        """Record the start of report generation"""
        self.log(
            action="report_start",
            stage="pending",
            details={
                "simulation_id": simulation_id,
                "graph_id": graph_id,
                "simulation_requirement": simulation_requirement,
                "message": "Report generation task started"
            }
        )

    def log_planning_start(self):
        """Record the start of outline planning"""
        self.log(
            action="planning_start",
            stage="planning",
            details={"message": "Started planning the report outline"}
        )

    def log_planning_context(self, context: Dict[str, Any]):
        """Record the context information gathered during planning"""
        self.log(
            action="planning_context",
            stage="planning",
            details={
                "message": "Gathered simulation context information",
                "context": context
            }
        )

    def log_planning_complete(self, outline_dict: Dict[str, Any]):
        """Record the completion of outline planning"""
        self.log(
            action="planning_complete",
            stage="planning",
            details={
                "message": "Outline planning complete",
                "outline": outline_dict
            }
        )

    def log_section_start(self, section_title: str, section_index: int):
        """Record the start of section generation"""
        self.log(
            action="section_start",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={"message": f"Started generating section: {section_title}"}
        )

    def log_react_thought(self, section_title: str, section_index: int, iteration: int, thought: str):
        """Record the ReACT reasoning process"""
        self.log(
            action="react_thought",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "thought": thought,
                "message": f"ReACT reasoning round {iteration}"
            }
        )
    
    def log_tool_call(
        self, 
        section_title: str, 
        section_index: int,
        tool_name: str, 
        parameters: Dict[str, Any],
        iteration: int
    ):
        """Record a tool call"""
        self.log(
            action="tool_call",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "tool_name": tool_name,
                "parameters": parameters,
                "message": f"Calling tool: {tool_name}"
            }
        )
    
    def log_tool_result(
        self,
        section_title: str,
        section_index: int,
        tool_name: str,
        result: str,
        iteration: int
    ):
        """Record a tool call result (full content, not truncated)"""
        self.log(
            action="tool_result",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "tool_name": tool_name,
                "result": result,  # Full result, not truncated
                "result_length": len(result),
                "message": f"Tool {tool_name} returned a result"
            }
        )
    
    def log_llm_response(
        self,
        section_title: str,
        section_index: int,
        response: str,
        iteration: int,
        has_tool_calls: bool,
        has_final_answer: bool
    ):
        """Record the LLM response (full content, not truncated)"""
        self.log(
            action="llm_response",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "iteration": iteration,
                "response": response,  # Full response, not truncated
                "response_length": len(response),
                "has_tool_calls": has_tool_calls,
                "has_final_answer": has_final_answer,
                "message": f"LLM response (tool calls: {has_tool_calls}, final answer: {has_final_answer})"
            }
        )
    
    def log_section_content(
        self,
        section_title: str,
        section_index: int,
        content: str,
        tool_calls_count: int
    ):
        """Record that section content generation finished (content only; does not mean the whole section is complete)"""
        self.log(
            action="section_content",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "content": content,  # Full content, not truncated
                "content_length": len(content),
                "tool_calls_count": tool_calls_count,
                "message": f"Content generation finished for section {section_title}"
            }
        )
    
    def log_section_full_complete(
        self,
        section_title: str,
        section_index: int,
        full_content: str
    ):
        """
        Record that a section finished generating

        Emits a section_complete action so a consumer can tell a section is
        truly complete and retrieve its full content.
        """
        self.log(
            action="section_complete",
            stage="generating",
            section_title=section_title,
            section_index=section_index,
            details={
                "content": full_content,
                "content_length": len(full_content),
                "message": f"Section {section_title} finished generating"
            }
        )

    def log_report_complete(self, total_sections: int, total_time_seconds: float):
        """Record that report generation completed"""
        self.log(
            action="report_complete",
            stage="completed",
            details={
                "total_sections": total_sections,
                "total_time_seconds": round(total_time_seconds, 2),
                "message": "Report generation complete"
            }
        )

    def log_error(self, error_message: str, stage: str, section_title: str = None):
        """Record an error"""
        self.log(
            action="error",
            stage=stage,
            section_title=section_title,
            section_index=None,
            details={
                "error": error_message,
                "message": f"An error occurred: {error_message}"
            }
        )


class ReportConsoleLogger:
    """
    Report Agent console logger

    Writes console-style logs (INFO, WARNING, etc.) to a console_log.txt file
    inside the report folder. Unlike agent_log.jsonl, these are plain-text
    console output.
    """

    def __init__(self, report_id: str):
        """
        Initialize the console logger

        Args:
            report_id: report ID, used to determine the log file path
        """
        self.report_id = report_id
        self.log_file_path = safe_join(
            Config.UPLOAD_FOLDER, 'reports', report_id, 'console_log.txt'
        )
        self._ensure_log_file()
        self._file_handler = None
        self._setup_file_handler()

    def _ensure_log_file(self):
        """Ensure the directory containing the log file exists"""
        log_dir = os.path.dirname(self.log_file_path)
        os.makedirs(log_dir, exist_ok=True)

    def _setup_file_handler(self):
        """Set up the file handler so logs are also written to the file"""
        import logging

        # Create the file handler
        self._file_handler = logging.FileHandler(
            self.log_file_path,
            mode='a',
            encoding='utf-8'
        )
        self._file_handler.setLevel(logging.INFO)

        # Use the same concise format as the console
        formatter = logging.Formatter(
            '[%(asctime)s] %(levelname)s: %(message)s',
            datefmt='%H:%M:%S'
        )
        self._file_handler.setFormatter(formatter)

        # Attach to the report_agent-related loggers
        loggers_to_attach = [
            'mirofish.report_agent',
            'mirofish.zep_tools',
        ]

        for logger_name in loggers_to_attach:
            target_logger = logging.getLogger(logger_name)
            # Avoid adding it twice
            if self._file_handler not in target_logger.handlers:
                target_logger.addHandler(self._file_handler)

    def close(self):
        """Close the file handler and remove it from the loggers"""
        import logging

        if self._file_handler:
            loggers_to_detach = [
                'mirofish.report_agent',
                'mirofish.zep_tools',
            ]

            for logger_name in loggers_to_detach:
                target_logger = logging.getLogger(logger_name)
                if self._file_handler in target_logger.handlers:
                    target_logger.removeHandler(self._file_handler)

            self._file_handler.close()
            self._file_handler = None

    def __del__(self):
        """Ensure the file handler is closed on destruction"""
        self.close()


class ReportStatus(str, Enum):
    """Report status"""
    PENDING = "pending"
    PLANNING = "planning"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ReportSection:
    """Report section"""
    title: str
    content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "content": self.content
        }

    def to_markdown(self, level: int = 2) -> str:
        """Convert to Markdown format"""
        md = f"{'#' * level} {self.title}\n\n"
        if self.content:
            md += f"{self.content}\n\n"
        return md


@dataclass
class ReportOutline:
    """Report outline"""
    title: str
    summary: str
    sections: List[ReportSection]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "sections": [s.to_dict() for s in self.sections]
        }

    def to_markdown(self) -> str:
        """Convert to Markdown format"""
        md = f"# {self.title}\n\n"
        md += f"> {self.summary}\n\n"
        for section in self.sections:
            md += section.to_markdown()
        return md


@dataclass
class Report:
    """Complete report"""
    report_id: str
    simulation_id: str
    graph_id: str
    simulation_requirement: str
    status: ReportStatus
    outline: Optional[ReportOutline] = None
    markdown_content: str = ""
    created_at: str = ""
    completed_at: str = ""
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "simulation_id": self.simulation_id,
            "graph_id": self.graph_id,
            "simulation_requirement": self.simulation_requirement,
            "status": self.status.value,
            "outline": self.outline.to_dict() if self.outline else None,
            "markdown_content": self.markdown_content,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error
        }


# ═══════════════════════════════════════════════════════════════
# Prompt template constants
# ═══════════════════════════════════════════════════════════════

# ── Tool descriptions ──

TOOL_DESC_INSIGHT_FORGE = """\
[Deep Insight Retrieval - a powerful retrieval tool]
This is our powerful retrieval function, purpose-built for deep analysis. It will:
1. Automatically break your question down into multiple sub-questions
2. Retrieve information from the simulation graph along multiple dimensions
3. Combine the results of semantic search, entity analysis, and relationship-chain tracing
4. Return the most comprehensive and in-depth retrieval content

[When to use]
- You need to analyze a topic in depth
- You need to understand multiple facets of a shopper's abandonment behavior
- You need rich material to support a report section

[What it returns]
- Relevant verbatim facts (can be quoted directly)
- Core entity insights
- Relationship-chain analysis"""

TOOL_DESC_PANORAMA_SEARCH = """\
[Broad Search - get the full picture]
This tool retrieves the complete picture of the simulation results and is especially
good for understanding how shopper sentiment evolved. It will:
1. Retrieve all relevant nodes and relationships
2. Distinguish currently valid facts from historical/expired facts
3. Help you understand how shopper sentiment evolved over the simulated session

[When to use]
- You need the full timeline of how the abandonment unfolded
- You need to compare shopper sentiment across different stages
- You need comprehensive entity and relationship information

[What it returns]
- Currently valid facts (the latest simulation results)
- Historical/expired facts (the evolution record)
- All entities involved"""

TOOL_DESC_QUICK_SEARCH = """\
[Simple Search - fast retrieval]
A lightweight, fast retrieval tool, good for simple, direct information lookups.

[When to use]
- You need to quickly look up a specific piece of information
- You need to verify a particular fact
- Simple information retrieval

[What it returns]
- A list of facts most relevant to the query"""

TOOL_DESC_INTERVIEW_AGENTS = """\
[Deep Interview - real agent interviews (dual platform)]
Runs real interviews with the running simulated shopper agents in-process via
SimulationRunner.interview_agents_batch() (IPC to the OASIS environment — no HTTP).
This is not an LLM mock-up; it gets the raw answers straight from the simulated
shopper personas running in the OASIS processes.
By default it interviews on both the Twitter and Reddit platforms simultaneously
to gather a more well-rounded set of viewpoints.

Workflow:
1. Automatically reads the persona file to learn about all simulated shopper agents
2. Intelligently selects the agents most relevant to the interview topic (e.g. price-sensitive shopper, comparison shopper, first-time buyer, brand-loyal customer)
3. Automatically generates interview questions
4. Runs the real agent interviews in-process on both platforms
5. Combines all interview results to provide a multi-perspective analysis

[When to use]
- You need to understand the abandonment from different shopper viewpoints (How does a price-sensitive shopper see it? How does a comparison shopper see it? What does a first-time buyer say?)
- You need to collect viewpoints and positions from multiple shopper types
- You need the simulated agents' real answers (from the OASIS simulation environment)
- You want to make the report more vivid by including an "interview transcript"

[What it returns]
- Identity information of the interviewed agents
- Each agent's interview answers on both the Twitter and Reddit platforms
- Key quotes (can be cited directly)
- An interview summary and comparison of viewpoints

[Important] The OASIS simulation environment must be running to use this feature!"""

# ── Outline planning prompt ──

PLAN_SYSTEM_PROMPT = """\
You are an expert author of "cart-abandonment recovery insight reports" for Vakaru, a Shopify cart-recovery product. You have a "god's-eye view" of the simulated world and can observe the behavior, statements, and interactions of every shopper agent in the simulation.

[Core idea]
We built a small simulated social world of shopper personas and injected a specific "simulation requirement" into it as the variable. The way that simulated world evolves is our prediction of why this shopper abandoned their cart and how to win them back. What you are observing is not "experimental data" but a rehearsal of the shopper's likely reactions.

[Your task]
Write a cart-abandonment recovery insight report that answers:
1. Under the given conditions, why did this shopper most likely abandon their cart?
2. How did the different shopper personas (price-sensitive, comparison-shopping, first-time buyer, brand-loyal, etc.) react and behave?
3. What objections, barriers, and the most effective recovery messaging angle does this simulation reveal?

[Report framing]
- This is a simulation-based abandonment-recovery insight report that reveals "given these conditions, why the shopper left and how to bring them back"
- Focus on the findings: the likely abandonment reason, the shopper personas' reactions, emerging patterns, and key objections
- The agents' statements and behavior in the simulated world are predictions of how a real shopper would react
- It is not an analysis of generic real-world public opinion
- It is not a vague, generic social-media sentiment summary

[Section count limit]
- At least 2 sections, at most 5 sections
- No sub-sections; write the full content directly within each section
- Keep the content concise and focused on the core recovery findings
- You design the section structure yourself based on the findings

Output the report outline in JSON format, in the following format:
{
    "title": "Report title",
    "summary": "Report summary (one sentence capturing the core recovery finding)",
    "sections": [
        {
            "title": "Section title",
            "description": "Description of the section content"
        }
    ]
}

Note: the sections array must have at least 2 and at most 5 elements!
Write the entire report in English."""

PLAN_USER_PROMPT_TEMPLATE = """\
[Scenario setup]
The variable (simulation requirement) we injected into the simulated world: {simulation_requirement}

[Simulated world scale]
- Number of entities participating in the simulation: {total_nodes}
- Number of relationships produced between entities: {total_edges}
- Entity type distribution: {entity_types}
- Number of active agents: {total_entities}

[A sample of the facts the simulation predicted]
{related_facts_json}

Review this simulation from a "god's-eye view":
1. Under the given conditions, what state did the shopper's situation end up in?
2. How did the different shopper personas (agents) react and behave?
3. What noteworthy abandonment drivers, objections, and recovery angles does this simulation reveal?

Based on the findings, design the most appropriate section structure for the report.

[Reminder] Report section count: at least 2, at most 5, with concise content focused on the core recovery findings. Write the report in English."""

# ── Section generation prompt ──

SECTION_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert author of cart-abandonment recovery insight reports for Vakaru, currently writing one section of the report.

Report title: {report_title}
Report summary: {report_summary}
Scenario (simulation requirement): {simulation_requirement}

The section you are currently writing: {section_title}

═══════════════════════════════════════════════════════════════
[Core idea]
═══════════════════════════════════════════════════════════════

The simulated world is a rehearsal of how the shopper reacts. We injected specific
conditions (the simulation requirement) into the simulated world, and the behavior
and interactions of the shopper agents in the simulation are predictions of how a
real shopper would react to the store, product, price, shipping, trust signals, and checkout.

Your task is to:
- Reveal, under the given conditions, why the shopper most likely abandoned their cart
- Predict how the different shopper personas (agents) reacted and behaved
- Surface the key objections, barriers, and recovery opportunities worth acting on

Do not write this as an analysis of generic real-world public opinion.
Stay focused on the shopper's likely behavior — the simulation results are the prediction.

═══════════════════════════════════════════════════════════════
[Most important rules - must be followed]
═══════════════════════════════════════════════════════════════

1. [You must call tools to observe the simulated world]
   - You are observing the rehearsal from a "god's-eye view"
   - All content must come from events and agent behavior that occurred in the simulated world
   - You may not write report content from your own knowledge
   - Call tools at least 3 times (at most 5) per section to observe the simulated world, which represents how the shopper reacts

2. [You must quote the agents' original statements and behavior]
   - The agents' statements and behavior are predictions of how a real shopper would react
   - Show these predictions in the report using quote format, for example:
     > "A price-sensitive shopper would say: $18.99 shipping on a $45 order felt excessive..."
   - These quotes are the core evidence of the simulation's prediction

3. [Language consistency - quoted content must be in the report language]
   - The content returned by the tools may contain mixed-language phrasing
   - The report must be written entirely in English
   - When you quote tool output that is not in English, translate it into fluent English before writing it into the report
   - Preserve the original meaning when translating, and make sure the phrasing is natural and smooth
   - This rule applies to both the body text and the quote blocks (> format)

4. [Faithfully present the findings]
   - The report content must reflect the simulation results in the simulated world
   - Do not add information that does not exist in the simulation
   - If information on some aspect is insufficient, say so honestly

═══════════════════════════════════════════════════════════════
[⚠️ Formatting rules - extremely important!]
═══════════════════════════════════════════════════════════════

[One section = the smallest content unit]
- Each section is the smallest chunk unit of the report
- Do not use any Markdown headings inside a section (#, ##, ###, ####, etc.)
- Do not add a section main title at the start of the content
- The section title is added automatically by the system; you only write the pure body content
- Use **bold**, paragraph breaks, quotes, and lists to organize content, but do not use headings

[Correct example]
```
This section analyzes why the shopper abandoned their cart. Through a deep analysis of the simulation data, we found...

**Checkout friction stage**

The shipping cost shown at checkout became the first turning point in the shopper's decision:

> "$18.99 shipping on a $45 order felt like too much..."

**Hesitation and comparison stage**

The shopper then began comparing the price against other stores:

- Strong price sensitivity
- High likelihood of comparison shopping
```

[Incorrect example]
```
## Executive summary     ← Wrong! Do not add any heading
### 1. Checkout stage    ← Wrong! Do not use ### for subsections
#### 1.1 Detailed analysis  ← Wrong! Do not use #### to subdivide

This section analyzes...
```

═══════════════════════════════════════════════════════════════
[Available retrieval tools] (call 3-5 times per section)
═══════════════════════════════════════════════════════════════

{tools_description}

[Tool usage advice - please mix different tools, do not use only one]
- insight_forge: deep insight analysis; automatically decomposes the question and retrieves facts and relationships along multiple dimensions
- panorama_search: wide-angle panoramic search; understand the full picture, timeline, and evolution
- quick_search: quickly verify a specific piece of information
- interview_agents: interview the simulated shopper agents to get different personas' first-person viewpoints and real reactions

═══════════════════════════════════════════════════════════════
[Workflow]
═══════════════════════════════════════════════════════════════

Each reply may do only one of the following two things (never both at once):

Option A - Call a tool:
Output your reasoning, then call one tool in the following format:
<tool_call>
{{"name": "tool_name", "parameters": {{"param_name": "param_value"}}}}
</tool_call>
The system will execute the tool and return the result to you. You do not need to and may not write the tool result yourself.

Option B - Output the final content:
Once you have gathered enough information through the tools, output the section content starting with "Final Answer:".

⚠️ Strictly forbidden:
- Do not include both a tool call and a Final Answer in a single reply
- Do not fabricate tool results (the Observation) yourself; all tool results are injected by the system
- At most one tool call per reply

═══════════════════════════════════════════════════════════════
[Section content requirements]
═══════════════════════════════════════════════════════════════

1. The content must be based on the simulation data retrieved by the tools
2. Quote source text extensively to demonstrate the simulation results
3. Use Markdown format (but no headings):
   - Use **bold text** to mark key points (in place of sub-headings)
   - Use lists (- or 1. 2. 3.) to organize points
   - Use blank lines to separate paragraphs
   - Do not use any heading syntax such as #, ##, ###, ####
4. [Quote format rules - quotes must stand alone as their own paragraph]
   A quote must be its own paragraph, with one blank line before and after; it cannot be mixed into a paragraph:

   ✅ Correct format:
   ```
   The shopper felt the shipping cost was unreasonable.

   > "$18.99 shipping on a $45 order felt rigid and unfair."

   This reflects the shopper's general dissatisfaction with the checkout.
   ```

   ❌ Incorrect format:
   ```
   The shopper felt the shipping cost was unreasonable.> "$18.99 shipping..." This reflects...
   ```
5. Maintain logical coherence with the other sections
6. [Avoid repetition] Carefully read the already-completed section content below and do not repeat the same information
7. [Reminder] Do not add any headings! Use **bold** in place of sub-section titles"""

SECTION_USER_PROMPT_TEMPLATE = """\
Already-completed section content (please read carefully to avoid repetition):
{previous_content}

═══════════════════════════════════════════════════════════════
[Current task] Write the section: {section_title}
═══════════════════════════════════════════════════════════════

[Important reminders]
1. Carefully read the already-completed sections above and avoid repeating the same content!
2. Before you begin, you must first call a tool to retrieve simulation data
3. Please mix different tools, do not use only one
4. The report content must come from the retrieval results; do not use your own knowledge

[⚠️ Formatting warning - must be followed]
- Do not write any heading (#, ##, ###, #### are all forbidden)
- Do not write "{section_title}" as the opening line
- The section title is added automatically by the system
- Write the body directly, using **bold** in place of sub-section titles

Please begin:
1. First reason (Thought) about what information this section needs
2. Then call a tool (Action) to retrieve simulation data
3. After gathering enough information, output the Final Answer (pure body text, no headings)
Write the section in English."""

# ── ReACT loop message templates ──

REACT_OBSERVATION_TEMPLATE = """\
Observation (retrieval result):

═══ Tool {tool_name} returned ═══
{result}

═══════════════════════════════════════════════════════════════
Tools called {tool_calls_count}/{max_tool_calls} times (already used: {used_tools_str}){unused_hint}
- If the information is sufficient: output the section content starting with "Final Answer:" (you must quote the source text above)
- If you need more information: call one tool to continue retrieving
═══════════════════════════════════════════════════════════════"""

REACT_INSUFFICIENT_TOOLS_MSG = (
    "[Note] You have only called tools {tool_calls_count} times; at least {min_tool_calls} are required. "
    "Please call a tool to retrieve more simulation data before outputting the Final Answer. {unused_hint}"
)

REACT_INSUFFICIENT_TOOLS_MSG_ALT = (
    "You have only called tools {tool_calls_count} times so far; at least {min_tool_calls} are required. "
    "Please call a tool to retrieve simulation data. {unused_hint}"
)

REACT_TOOL_LIMIT_MSG = (
    "The tool-call limit has been reached ({tool_calls_count}/{max_tool_calls}); you cannot call any more tools. "
    'Based on the information already gathered, immediately output the section content starting with "Final Answer:".'
)

REACT_UNUSED_TOOLS_HINT = "\n💡 You have not yet used: {unused_list}. Consider trying a different tool to get multiple perspectives."

REACT_FORCE_FINAL_MSG = "The tool-call limit has been reached. Please output Final Answer: directly and generate the section content."

# ── Chat prompt ──

CHAT_SYSTEM_PROMPT_TEMPLATE = """\
You are a concise, efficient cart-recovery simulation assistant.

[Background]
Simulation requirement: {simulation_requirement}

[The analysis report already generated]
{report_content}

[Rules]
1. Answer questions based primarily on the report content above
2. Answer directly; avoid long-winded reasoning
3. Only call a tool to retrieve more data when the report content is insufficient to answer
4. Keep answers concise, clear, and well-organized

[Available tools] (use only when needed, at most 1-2 calls)
{tools_description}

[Tool-call format]
<tool_call>
{{"name": "tool_name", "parameters": {{"param_name": "param_value"}}}}
</tool_call>

[Answer style]
- Concise and direct, no rambling
- Use the > format to quote key content
- Lead with the conclusion, then explain why
Answer in English."""

CHAT_OBSERVATION_SUFFIX = "\n\nPlease answer the question concisely."

# ═══════════════════════════════════════════════════════════════
# ReportAgent main class
# ═══════════════════════════════════════════════════════════════


class ReportAgent:
    """
    Report Agent - the simulation report generation agent

    Uses the ReACT (Reasoning + Acting) pattern:
    1. Planning stage: analyze the simulation requirement and plan the report outline structure
    2. Generation stage: generate content section by section; each section may call tools multiple times to gather information
    3. Reflection stage: check the completeness and accuracy of the content
    """

    # Maximum number of tool calls (per section)
    MAX_TOOL_CALLS_PER_SECTION = 5

    # Maximum number of reflection rounds
    MAX_REFLECTION_ROUNDS = 3

    # Maximum number of tool calls in a chat
    MAX_TOOL_CALLS_PER_CHAT = 2

    def __init__(
        self,
        graph_id: str,
        simulation_id: str,
        simulation_requirement: str,
        llm_client: Optional[LLMClient] = None,
        zep_tools: Optional[ZepToolsService] = None
    ):
        """
        Initialize the Report Agent

        Args:
            graph_id: graph ID
            simulation_id: simulation ID
            simulation_requirement: description of the simulation requirement
            llm_client: LLM client (optional)
            zep_tools: Zep tools service (optional)
        """
        self.graph_id = graph_id
        self.simulation_id = simulation_id
        self.simulation_requirement = simulation_requirement

        self.llm = llm_client or LLMClient()
        self.zep_tools = zep_tools or ZepToolsService()

        # Tool definitions
        self.tools = self._define_tools()

        # Logger (initialized in generate_report)
        self.report_logger: Optional[ReportLogger] = None
        # Console logger (initialized in generate_report)
        self.console_logger: Optional[ReportConsoleLogger] = None

        logger.info(f"ReportAgent initialized: graph_id={graph_id}, simulation_id={simulation_id}")

    def _define_tools(self) -> Dict[str, Dict[str, Any]]:
        """Define the available tools"""
        return {
            "insight_forge": {
                "name": "insight_forge",
                "description": TOOL_DESC_INSIGHT_FORGE,
                "parameters": {
                    "query": "The question or topic you want to analyze in depth",
                    "report_context": "Context of the current report section (optional; helps generate more precise sub-questions)"
                }
            },
            "panorama_search": {
                "name": "panorama_search",
                "description": TOOL_DESC_PANORAMA_SEARCH,
                "parameters": {
                    "query": "Search query, used for relevance ranking",
                    "include_expired": "Whether to include expired/historical content (default True)"
                }
            },
            "quick_search": {
                "name": "quick_search",
                "description": TOOL_DESC_QUICK_SEARCH,
                "parameters": {
                    "query": "Search query string",
                    "limit": "Number of results to return (optional, default 10)"
                }
            },
            "interview_agents": {
                "name": "interview_agents",
                "description": TOOL_DESC_INTERVIEW_AGENTS,
                "parameters": {
                    "interview_topic": "The interview topic or requirement (e.g. 'Understand how a price-sensitive shopper feels about the shipping cost at checkout')",
                    "max_agents": "Maximum number of agents to interview (optional, default 5, max 10)"
                }
            }
        }
    
    def _execute_tool(self, tool_name: str, parameters: Dict[str, Any], report_context: str = "") -> str:
        """
        Execute a tool call

        Args:
            tool_name: tool name
            parameters: tool parameters
            report_context: report context (used by InsightForge)

        Returns:
            Tool execution result (text format)
        """
        logger.info(f"Executing tool: {tool_name}, parameters: {parameters}")

        try:
            if tool_name == "insight_forge":
                query = parameters.get("query", "")
                ctx = parameters.get("report_context", "") or report_context
                result = self.zep_tools.insight_forge(
                    graph_id=self.graph_id,
                    query=query,
                    simulation_requirement=self.simulation_requirement,
                    report_context=ctx
                )
                return result.to_text()
            
            elif tool_name == "panorama_search":
                # Broad search - get the full picture
                query = parameters.get("query", "")
                include_expired = parameters.get("include_expired", True)
                if isinstance(include_expired, str):
                    include_expired = include_expired.lower() in ['true', '1', 'yes']
                result = self.zep_tools.panorama_search(
                    graph_id=self.graph_id,
                    query=query,
                    include_expired=include_expired
                )
                return result.to_text()
            
            elif tool_name == "quick_search":
                # Simple search - fast retrieval
                query = parameters.get("query", "")
                limit = parameters.get("limit", 10)
                if isinstance(limit, str):
                    limit = int(limit)
                result = self.zep_tools.quick_search(
                    graph_id=self.graph_id,
                    query=query,
                    limit=limit
                )
                return result.to_text()
            
            elif tool_name == "interview_agents":
                # Deep interview - call the real OASIS interview API to get the simulated agents' answers (dual platform)
                interview_topic = parameters.get("interview_topic", parameters.get("query", ""))
                max_agents = parameters.get("max_agents", 5)
                if isinstance(max_agents, str):
                    max_agents = int(max_agents)
                max_agents = min(max_agents, 10)
                result = self.zep_tools.interview_agents(
                    simulation_id=self.simulation_id,
                    interview_requirement=interview_topic,
                    simulation_requirement=self.simulation_requirement,
                    max_agents=max_agents
                )
                return result.to_text()
            
            # ========== Backward-compatible legacy tools (internally redirected to the new tools) ==========

            elif tool_name == "search_graph":
                # Redirect to quick_search
                logger.info("search_graph has been redirected to quick_search")
                return self._execute_tool("quick_search", parameters, report_context)
            
            elif tool_name == "get_graph_statistics":
                result = self.zep_tools.get_graph_statistics(self.graph_id)
                return json.dumps(result, ensure_ascii=False, indent=2)
            
            elif tool_name == "get_entity_summary":
                entity_name = parameters.get("entity_name", "")
                result = self.zep_tools.get_entity_summary(
                    graph_id=self.graph_id,
                    entity_name=entity_name
                )
                return json.dumps(result, ensure_ascii=False, indent=2)
            
            elif tool_name == "get_simulation_context":
                # Redirect to insight_forge, since it is more powerful
                logger.info("get_simulation_context has been redirected to insight_forge")
                query = parameters.get("query", self.simulation_requirement)
                return self._execute_tool("insight_forge", {"query": query}, report_context)
            
            elif tool_name == "get_entities_by_type":
                entity_type = parameters.get("entity_type", "")
                nodes = self.zep_tools.get_entities_by_type(
                    graph_id=self.graph_id,
                    entity_type=entity_type
                )
                result = [n.to_dict() for n in nodes]
                return json.dumps(result, ensure_ascii=False, indent=2)
            
            else:
                return f"Unknown tool: {tool_name}. Please use one of the following tools: insight_forge, panorama_search, quick_search"

        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name}, error: {str(e)}")
            return f"Tool execution failed: {str(e)}"

    # Set of valid tool names, used to validate bare-JSON fallback parsing
    VALID_TOOL_NAMES = {"insight_forge", "panorama_search", "quick_search", "interview_agents"}

    def _parse_tool_calls(self, response: str) -> List[Dict[str, Any]]:
        """
        Parse tool calls from the LLM response

        Supported formats (in priority order):
        1. <tool_call>{"name": "tool_name", "parameters": {...}}</tool_call>
        2. Bare JSON (the whole response, or a single line, is a tool-call JSON)
        """
        tool_calls = []

        # Format 1: XML style (standard format)
        xml_pattern = r'<tool_call>\s*(\{.*?\})\s*</tool_call>'
        for match in re.finditer(xml_pattern, response, re.DOTALL):
            try:
                call_data = json.loads(match.group(1))
                tool_calls.append(call_data)
            except json.JSONDecodeError:
                pass

        if tool_calls:
            return tool_calls

        # Format 2: fallback - the LLM directly outputs bare JSON (no <tool_call> wrapper)
        # Only attempted when format 1 did not match, to avoid mis-matching JSON in the body
        stripped = response.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            try:
                call_data = json.loads(stripped)
                if self._is_valid_tool_call(call_data):
                    tool_calls.append(call_data)
                    return tool_calls
            except json.JSONDecodeError:
                pass

        # The response may contain reasoning text + bare JSON; try to extract the last JSON object
        json_pattern = r'(\{"(?:name|tool)"\s*:.*?\})\s*$'
        match = re.search(json_pattern, stripped, re.DOTALL)
        if match:
            try:
                call_data = json.loads(match.group(1))
                if self._is_valid_tool_call(call_data):
                    tool_calls.append(call_data)
            except json.JSONDecodeError:
                pass

        return tool_calls

    def _is_valid_tool_call(self, data: dict) -> bool:
        """Validate whether the parsed JSON is a valid tool call"""
        # Supports both {"name": ..., "parameters": ...} and {"tool": ..., "params": ...} key names
        tool_name = data.get("name") or data.get("tool")
        if tool_name and tool_name in self.VALID_TOOL_NAMES:
            # Normalize the key names to name / parameters
            if "tool" in data:
                data["name"] = data.pop("tool")
            if "params" in data and "parameters" not in data:
                data["parameters"] = data.pop("params")
            return True
        return False

    def _get_tools_description(self) -> str:
        """Generate the tool description text"""
        desc_parts = ["Available tools:"]
        for name, tool in self.tools.items():
            params_desc = ", ".join([f"{k}: {v}" for k, v in tool["parameters"].items()])
            desc_parts.append(f"- {name}: {tool['description']}")
            if params_desc:
                desc_parts.append(f"  Parameters: {params_desc}")
        return "\n".join(desc_parts)
    
    def plan_outline(
        self, 
        progress_callback: Optional[Callable] = None
    ) -> ReportOutline:
        """
        Plan the report outline

        Uses the LLM to analyze the simulation requirement and plan the report's outline structure

        Args:
            progress_callback: progress callback function

        Returns:
            ReportOutline: the report outline
        """
        logger.info("Starting to plan the report outline...")

        if progress_callback:
            progress_callback("planning", 0, "Analyzing the simulation requirement...")

        # First get the simulation context
        context = self.zep_tools.get_simulation_context(
            graph_id=self.graph_id,
            simulation_requirement=self.simulation_requirement
        )

        if progress_callback:
            progress_callback("planning", 30, "Generating the report outline...")
        
        system_prompt = PLAN_SYSTEM_PROMPT
        user_prompt = PLAN_USER_PROMPT_TEMPLATE.format(
            simulation_requirement=self.simulation_requirement,
            total_nodes=context.get('graph_statistics', {}).get('total_nodes', 0),
            total_edges=context.get('graph_statistics', {}).get('total_edges', 0),
            entity_types=list(context.get('graph_statistics', {}).get('entity_types', {}).keys()),
            total_entities=context.get('total_entities', 0),
            related_facts_json=json.dumps(context.get('related_facts', [])[:10], ensure_ascii=False, indent=2),
        )

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3
            )
            
            if progress_callback:
                progress_callback("planning", 80, "Parsing the outline structure...")

            # Parse the outline
            sections = []
            for section_data in response.get("sections", []):
                sections.append(ReportSection(
                    title=section_data.get("title", ""),
                    content=""
                ))

            outline = ReportOutline(
                title=response.get("title", "Cart Abandonment Analysis Report"),
                summary=response.get("summary", ""),
                sections=sections
            )

            if progress_callback:
                progress_callback("planning", 100, "Outline planning complete")

            logger.info(f"Outline planning complete: {len(sections)} sections")
            return outline

        except Exception as e:
            logger.error(f"Outline planning failed: {str(e)}")
            # Return a default outline (3 sections, as a fallback)
            return ReportOutline(
                title="Cart Abandonment Recovery Report",
                summary="Predicted abandonment drivers and recovery opportunities based on the shopper simulation",
                sections=[
                    ReportSection(title="Scenario and Core Findings"),
                    ReportSection(title="Shopper Behavior and Objection Analysis"),
                    ReportSection(title="Recommended Recovery Angle and Risk Notes")
                ]
            )
    
    def _generate_section_react(
        self, 
        section: ReportSection,
        outline: ReportOutline,
        previous_sections: List[str],
        progress_callback: Optional[Callable] = None,
        section_index: int = 0
    ) -> str:
        """
        Generate a single section's content using the ReACT pattern

        ReACT loop:
        1. Thought - analyze what information is needed
        2. Action - call a tool to retrieve information
        3. Observation - analyze the tool's returned result
        4. Repeat until the information is sufficient or the max count is reached
        5. Final Answer - generate the section content

        Args:
            section: the section to generate
            outline: the complete outline
            previous_sections: the content of earlier sections (used to maintain coherence)
            progress_callback: progress callback
            section_index: section index (used for logging)

        Returns:
            The section content (Markdown format)
        """
        logger.info(f"ReACT generating section: {section.title}")

        # Record the section-start log
        if self.report_logger:
            self.report_logger.log_section_start(section.title, section_index)
        
        system_prompt = SECTION_SYSTEM_PROMPT_TEMPLATE.format(
            report_title=outline.title,
            report_summary=outline.summary,
            simulation_requirement=self.simulation_requirement,
            section_title=section.title,
            tools_description=self._get_tools_description(),
        )

        # Build the user prompt - pass in at most 4000 characters per completed section
        if previous_sections:
            previous_parts = []
            for sec in previous_sections:
                # At most 4000 characters per section
                truncated = sec[:4000] + "..." if len(sec) > 4000 else sec
                previous_parts.append(truncated)
            previous_content = "\n\n---\n\n".join(previous_parts)
        else:
            previous_content = "(This is the first section)"

        user_prompt = SECTION_USER_PROMPT_TEMPLATE.format(
            previous_content=previous_content,
            section_title=section.title,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        # ReACT loop
        tool_calls_count = 0
        max_iterations = 5  # Maximum number of iterations
        min_tool_calls = 3  # Minimum number of tool calls
        conflict_retries = 0  # Count of consecutive conflicts where a tool call and Final Answer appear together
        used_tools = set()  # Track which tool names have been called
        all_tools = {"insight_forge", "panorama_search", "quick_search", "interview_agents"}

        # Report context, used for InsightForge's sub-question generation
        report_context = f"Section title: {section.title}\nSimulation requirement: {self.simulation_requirement}"

        for iteration in range(max_iterations):
            if progress_callback:
                progress_callback(
                    "generating",
                    int((iteration / max_iterations) * 100),
                    f"Deep retrieval and writing in progress ({tool_calls_count}/{self.MAX_TOOL_CALLS_PER_SECTION})"
                )

            # Call the LLM
            response = self.llm.chat(
                messages=messages,
                temperature=0.5,
                max_tokens=4096
            )

            # Check whether the LLM returned None (API error or empty content)
            if response is None:
                logger.warning(f"Section {section.title} iteration {iteration + 1}: LLM returned None")
                # If iterations remain, add messages and retry
                if iteration < max_iterations - 1:
                    messages.append({"role": "assistant", "content": "(empty response)"})
                    messages.append({"role": "user", "content": "Please continue generating content."})
                    continue
                # The last iteration also returned None; break out of the loop into the forced finalization
                break

            logger.debug(f"LLM response: {response[:200]}...")

            # Parse once and reuse the result
            tool_calls = self._parse_tool_calls(response)
            has_tool_calls = bool(tool_calls)
            has_final_answer = "Final Answer:" in response

            # ── Conflict handling: the LLM output both a tool call and a Final Answer ──
            if has_tool_calls and has_final_answer:
                conflict_retries += 1
                logger.warning(
                    f"Section {section.title} round {iteration+1}: "
                    f"LLM output both a tool call and a Final Answer (conflict #{conflict_retries})"
                )

                if conflict_retries <= 2:
                    # First two times: discard this response and ask the LLM to reply again
                    messages.append({"role": "assistant", "content": response})
                    messages.append({
                        "role": "user",
                        "content": (
                            "[Format error] You included both a tool call and a Final Answer in a single reply, which is not allowed.\n"
                            "Each reply may do only one of the following two things:\n"
                            "- Call one tool (output a single <tool_call> block, do not write Final Answer)\n"
                            "- Output the final content (start with 'Final Answer:', do not include <tool_call>)\n"
                            "Please reply again, doing only one of them."
                        ),
                    })
                    continue
                else:
                    # Third time: degrade by truncating to the first tool call and forcing execution
                    logger.warning(
                        f"Section {section.title}: {conflict_retries} consecutive conflicts, "
                        "degrading to truncate and execute the first tool call"
                    )
                    first_tool_end = response.find('</tool_call>')
                    if first_tool_end != -1:
                        response = response[:first_tool_end + len('</tool_call>')]
                        tool_calls = self._parse_tool_calls(response)
                        has_tool_calls = bool(tool_calls)
                    has_final_answer = False
                    conflict_retries = 0

            # Record the LLM response log
            if self.report_logger:
                self.report_logger.log_llm_response(
                    section_title=section.title,
                    section_index=section_index,
                    response=response,
                    iteration=iteration + 1,
                    has_tool_calls=has_tool_calls,
                    has_final_answer=has_final_answer
                )

            # ── Case 1: the LLM output a Final Answer ──
            if has_final_answer:
                # Too few tool calls; reject and ask it to keep calling tools
                if tool_calls_count < min_tool_calls:
                    messages.append({"role": "assistant", "content": response})
                    unused_tools = all_tools - used_tools
                    unused_hint = f"(These tools have not been used yet; consider using them: {', '.join(unused_tools)})" if unused_tools else ""
                    messages.append({
                        "role": "user",
                        "content": REACT_INSUFFICIENT_TOOLS_MSG.format(
                            tool_calls_count=tool_calls_count,
                            min_tool_calls=min_tool_calls,
                            unused_hint=unused_hint,
                        ),
                    })
                    continue

                # Normal completion
                final_answer = response.split("Final Answer:")[-1].strip()
                logger.info(f"Section {section.title} finished generating (tool calls: {tool_calls_count})")

                if self.report_logger:
                    self.report_logger.log_section_content(
                        section_title=section.title,
                        section_index=section_index,
                        content=final_answer,
                        tool_calls_count=tool_calls_count
                    )
                return final_answer

            # ── Case 2: the LLM tried to call a tool ──
            if has_tool_calls:
                # Tool budget exhausted → tell it explicitly and ask it to output a Final Answer
                if tool_calls_count >= self.MAX_TOOL_CALLS_PER_SECTION:
                    messages.append({"role": "assistant", "content": response})
                    messages.append({
                        "role": "user",
                        "content": REACT_TOOL_LIMIT_MSG.format(
                            tool_calls_count=tool_calls_count,
                            max_tool_calls=self.MAX_TOOL_CALLS_PER_SECTION,
                        ),
                    })
                    continue

                # Only execute the first tool call
                call = tool_calls[0]
                if len(tool_calls) > 1:
                    logger.info(f"LLM tried to call {len(tool_calls)} tools; only executing the first: {call['name']}")

                if self.report_logger:
                    self.report_logger.log_tool_call(
                        section_title=section.title,
                        section_index=section_index,
                        tool_name=call["name"],
                        parameters=call.get("parameters", {}),
                        iteration=iteration + 1
                    )

                result = self._execute_tool(
                    call["name"],
                    call.get("parameters", {}),
                    report_context=report_context
                )

                if self.report_logger:
                    self.report_logger.log_tool_result(
                        section_title=section.title,
                        section_index=section_index,
                        tool_name=call["name"],
                        result=result,
                        iteration=iteration + 1
                    )

                tool_calls_count += 1
                used_tools.add(call['name'])

                # Build the unused-tools hint
                unused_tools = all_tools - used_tools
                unused_hint = ""
                if unused_tools and tool_calls_count < self.MAX_TOOL_CALLS_PER_SECTION:
                    unused_hint = REACT_UNUSED_TOOLS_HINT.format(unused_list=", ".join(unused_tools))

                messages.append({"role": "assistant", "content": response})
                messages.append({
                    "role": "user",
                    "content": REACT_OBSERVATION_TEMPLATE.format(
                        tool_name=call["name"],
                        result=result,
                        tool_calls_count=tool_calls_count,
                        max_tool_calls=self.MAX_TOOL_CALLS_PER_SECTION,
                        used_tools_str=", ".join(used_tools),
                        unused_hint=unused_hint,
                    ),
                })
                continue

            # ── Case 3: neither a tool call nor a Final Answer ──
            messages.append({"role": "assistant", "content": response})

            if tool_calls_count < min_tool_calls:
                # Too few tool calls; recommend the tools that have not been used
                unused_tools = all_tools - used_tools
                unused_hint = f"(These tools have not been used yet; consider using them: {', '.join(unused_tools)})" if unused_tools else ""

                messages.append({
                    "role": "user",
                    "content": REACT_INSUFFICIENT_TOOLS_MSG_ALT.format(
                        tool_calls_count=tool_calls_count,
                        min_tool_calls=min_tool_calls,
                        unused_hint=unused_hint,
                    ),
                })
                continue

            # Enough tool calls have been made; the LLM produced content but without the "Final Answer:" prefix.
            # Accept this content as the final answer directly, without spinning further.
            logger.info(f"Section {section.title}: no 'Final Answer:' prefix detected; accepting the LLM output as the final content directly (tool calls: {tool_calls_count})")
            final_answer = response.strip()

            if self.report_logger:
                self.report_logger.log_section_content(
                    section_title=section.title,
                    section_index=section_index,
                    content=final_answer,
                    tool_calls_count=tool_calls_count
                )
            return final_answer

        # Reached the maximum number of iterations; force content generation
        logger.warning(f"Section {section.title} reached the maximum number of iterations; forcing generation")
        messages.append({"role": "user", "content": REACT_FORCE_FINAL_MSG})

        response = self.llm.chat(
            messages=messages,
            temperature=0.5,
            max_tokens=4096
        )

        # Check whether the LLM returned None during the forced finalization
        if response is None:
            logger.error(f"Section {section.title}: LLM returned None during forced finalization; using a default error message")
            final_answer = f"(This section failed to generate: the LLM returned an empty response, please retry later)"
        elif "Final Answer:" in response:
            final_answer = response.split("Final Answer:")[-1].strip()
        else:
            final_answer = response

        # Record the section-content generation completion log
        if self.report_logger:
            self.report_logger.log_section_content(
                section_title=section.title,
                section_index=section_index,
                content=final_answer,
                tool_calls_count=tool_calls_count
            )
        
        return final_answer
    
    def generate_report(
        self, 
        progress_callback: Optional[Callable[[str, int, str], None]] = None,
        report_id: Optional[str] = None
    ) -> Report:
        """
        Generate the complete report (streamed section by section)

        Each section is saved to the folder as soon as it is generated, without
        waiting for the whole report to finish.
        File structure:
        reports/{report_id}/
            meta.json       - report metadata
            outline.json    - report outline
            progress.json   - generation progress
            section_01.md   - section 1
            section_02.md   - section 2
            ...
            full_report.md  - the complete report

        Args:
            progress_callback: progress callback function (stage, progress, message)
            report_id: report ID (optional; auto-generated if not provided)

        Returns:
            Report: the complete report
        """
        import uuid

        # If no report_id was passed in, generate one automatically
        if not report_id:
            report_id = f"report_{uuid.uuid4().hex[:12]}"
        start_time = datetime.now()
        
        report = Report(
            report_id=report_id,
            simulation_id=self.simulation_id,
            graph_id=self.graph_id,
            simulation_requirement=self.simulation_requirement,
            status=ReportStatus.PENDING,
            created_at=datetime.now().isoformat()
        )
        
        # List of completed section titles (used for progress tracking)
        completed_section_titles = []

        try:
            # Initialization: create the report folder and save the initial state
            ReportManager._ensure_report_folder(report_id)

            # Initialize the logger (structured log agent_log.jsonl)
            self.report_logger = ReportLogger(report_id)
            self.report_logger.log_start(
                simulation_id=self.simulation_id,
                graph_id=self.graph_id,
                simulation_requirement=self.simulation_requirement
            )

            # Initialize the console logger (console_log.txt)
            self.console_logger = ReportConsoleLogger(report_id)

            ReportManager.update_progress(
                report_id, "pending", 0, "Initializing the report...",
                completed_sections=[]
            )
            ReportManager.save_report(report)

            # Stage 1: plan the outline
            report.status = ReportStatus.PLANNING
            ReportManager.update_progress(
                report_id, "planning", 5, "Starting to plan the report outline...",
                completed_sections=[]
            )

            # Record the planning-start log
            self.report_logger.log_planning_start()

            if progress_callback:
                progress_callback("planning", 0, "Starting to plan the report outline...")

            outline = self.plan_outline(
                progress_callback=lambda stage, prog, msg: 
                    progress_callback(stage, prog // 5, msg) if progress_callback else None
            )
            report.outline = outline

            # Record the planning-complete log
            self.report_logger.log_planning_complete(outline.to_dict())

            # Save the outline to a file
            ReportManager.save_outline(report_id, outline)
            ReportManager.update_progress(
                report_id, "planning", 15, f"Outline planning complete, {len(outline.sections)} sections in total",
                completed_sections=[]
            )
            ReportManager.save_report(report)

            logger.info(f"Outline saved to file: {report_id}/outline.json")

            # Stage 2: generate section by section (saving each section)
            report.status = ReportStatus.GENERATING

            total_sections = len(outline.sections)
            generated_sections = []  # Keep the content for context

            for i, section in enumerate(outline.sections):
                section_num = i + 1
                base_progress = 20 + int((i / total_sections) * 70)

                # Update progress
                ReportManager.update_progress(
                    report_id, "generating", base_progress,
                    f"Generating section: {section.title} ({section_num}/{total_sections})",
                    current_section=section.title,
                    completed_sections=completed_section_titles
                )

                if progress_callback:
                    progress_callback(
                        "generating",
                        base_progress,
                        f"Generating section: {section.title} ({section_num}/{total_sections})"
                    )

                # Generate the main section content
                section_content = self._generate_section_react(
                    section=section,
                    outline=outline,
                    previous_sections=generated_sections,
                    progress_callback=lambda stage, prog, msg:
                        progress_callback(
                            stage, 
                            base_progress + int(prog * 0.7 / total_sections),
                            msg
                        ) if progress_callback else None,
                    section_index=section_num
                )
                
                section.content = section_content
                generated_sections.append(f"## {section.title}\n\n{section_content}")

                # Save the section
                ReportManager.save_section(report_id, section_num, section)
                completed_section_titles.append(section.title)

                # Record the section-complete log
                full_section_content = f"## {section.title}\n\n{section_content}"

                if self.report_logger:
                    self.report_logger.log_section_full_complete(
                        section_title=section.title,
                        section_index=section_num,
                        full_content=full_section_content.strip()
                    )

                logger.info(f"Section saved: {report_id}/section_{section_num:02d}.md")

                # Update progress
                ReportManager.update_progress(
                    report_id, "generating",
                    base_progress + int(70 / total_sections),
                    f"Section {section.title} completed",
                    current_section=None,
                    completed_sections=completed_section_titles
                )

            # Stage 3: assemble the complete report
            if progress_callback:
                progress_callback("generating", 95, "Assembling the complete report...")

            ReportManager.update_progress(
                report_id, "generating", 95, "Assembling the complete report...",
                completed_sections=completed_section_titles
            )

            # Use ReportManager to assemble the complete report
            report.markdown_content = ReportManager.assemble_full_report(report_id, outline)
            report.status = ReportStatus.COMPLETED
            report.completed_at = datetime.now().isoformat()

            # Compute the total elapsed time
            total_time_seconds = (datetime.now() - start_time).total_seconds()

            # Record the report-complete log
            if self.report_logger:
                self.report_logger.log_report_complete(
                    total_sections=total_sections,
                    total_time_seconds=total_time_seconds
                )
            
            # Save the final report
            ReportManager.save_report(report)
            ReportManager.update_progress(
                report_id, "completed", 100, "Report generation complete",
                completed_sections=completed_section_titles
            )

            if progress_callback:
                progress_callback("completed", 100, "Report generation complete")

            logger.info(f"Report generation complete: {report_id}")

            # Close the console logger
            if self.console_logger:
                self.console_logger.close()
                self.console_logger = None

            return report

        except Exception as e:
            logger.error(f"Report generation failed: {str(e)}")
            report.status = ReportStatus.FAILED
            report.error = str(e)

            # Record the error log
            if self.report_logger:
                self.report_logger.log_error(str(e), "failed")

            # Save the failed state
            try:
                ReportManager.save_report(report)
                ReportManager.update_progress(
                    report_id, "failed", -1, f"Report generation failed: {str(e)}",
                    completed_sections=completed_section_titles
                )
            except Exception:
                pass  # Ignore errors when saving fails

            # Close the console logger
            if self.console_logger:
                self.console_logger.close()
                self.console_logger = None

            return report
    
    def chat(
        self, 
        message: str,
        chat_history: List[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Chat with the Report Agent

        During the conversation the agent can autonomously call retrieval tools to answer questions

        Args:
            message: the user message
            chat_history: the conversation history

        Returns:
            {
                "response": "Agent reply",
                "tool_calls": [list of tools called],
                "sources": [information sources]
            }
        """
        logger.info(f"Report Agent chat: {message[:50]}...")

        chat_history = chat_history or []

        # Get the already-generated report content
        report_content = ""
        try:
            report = ReportManager.get_report_by_simulation(self.simulation_id)
            if report and report.markdown_content:
                # Limit the report length to avoid an overly long context
                report_content = report.markdown_content[:15000]
                if len(report.markdown_content) > 15000:
                    report_content += "\n\n... [report content truncated] ..."
        except Exception as e:
            logger.warning(f"Failed to get the report content: {e}")

        system_prompt = CHAT_SYSTEM_PROMPT_TEMPLATE.format(
            simulation_requirement=self.simulation_requirement,
            report_content=report_content if report_content else "(no report yet)",
            tools_description=self._get_tools_description(),
        )

        # Build the messages
        messages = [{"role": "system", "content": system_prompt}]

        # Add the conversation history
        for h in chat_history[-10:]:  # Limit the history length
            messages.append(h)

        # Add the user message
        messages.append({
            "role": "user",
            "content": message
        })

        # ReACT loop (simplified)
        tool_calls_made = []
        max_iterations = 2  # Reduce the number of iterations

        for iteration in range(max_iterations):
            response = self.llm.chat(
                messages=messages,
                temperature=0.5
            )

            # Parse tool calls
            tool_calls = self._parse_tool_calls(response)

            if not tool_calls:
                # No tool call; return the response directly
                clean_response = re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL)
                clean_response = re.sub(r'\[TOOL_CALL\].*?\)', '', clean_response)

                return {
                    "response": clean_response.strip(),
                    "tool_calls": tool_calls_made,
                    "sources": [tc.get("parameters", {}).get("query", "") for tc in tool_calls_made]
                }

            # Execute tool calls (limited number)
            tool_results = []
            for call in tool_calls[:1]:  # At most 1 tool call per round
                if len(tool_calls_made) >= self.MAX_TOOL_CALLS_PER_CHAT:
                    break
                result = self._execute_tool(call["name"], call.get("parameters", {}))
                tool_results.append({
                    "tool": call["name"],
                    "result": result[:1500]  # Limit the result length
                })
                tool_calls_made.append(call)

            # Add the results to the messages
            messages.append({"role": "assistant", "content": response})
            observation = "\n".join([f"[{r['tool']} result]\n{r['result']}" for r in tool_results])
            messages.append({
                "role": "user",
                "content": observation + CHAT_OBSERVATION_SUFFIX
            })

        # Reached the max iterations; get the final response
        final_response = self.llm.chat(
            messages=messages,
            temperature=0.5
        )

        # Clean the response
        clean_response = re.sub(r'<tool_call>.*?</tool_call>', '', final_response, flags=re.DOTALL)
        clean_response = re.sub(r'\[TOOL_CALL\].*?\)', '', clean_response)
        
        return {
            "response": clean_response.strip(),
            "tool_calls": tool_calls_made,
            "sources": [tc.get("parameters", {}).get("query", "") for tc in tool_calls_made]
        }


class ReportManager:
    """
    Report manager

    Responsible for the persistent storage and retrieval of reports

    File structure (section-by-section output):
    reports/
      {report_id}/
        meta.json          - report metadata and status
        outline.json       - report outline
        progress.json      - generation progress
        section_01.md      - section 1
        section_02.md      - section 2
        ...
        full_report.md     - the complete report
    """

    # Report storage directory
    REPORTS_DIR = os.path.join(Config.UPLOAD_FOLDER, 'reports')

    @classmethod
    def _ensure_reports_dir(cls):
        """Ensure the report root directory exists"""
        os.makedirs(cls.REPORTS_DIR, exist_ok=True)

    @classmethod
    def _get_report_folder(cls, report_id: str) -> str:
        """Get the report folder path (containment-checked, #13).

        Chokepoint for the per-report files (_get_report_path / _get_*_path all
        route through it), so safe_join here covers every report read/write."""
        return safe_join(cls.REPORTS_DIR, report_id)

    @classmethod
    def _ensure_report_folder(cls, report_id: str) -> str:
        """Ensure the report folder exists and return its path"""
        folder = cls._get_report_folder(report_id)
        os.makedirs(folder, exist_ok=True)
        return folder

    @classmethod
    def _get_report_path(cls, report_id: str) -> str:
        """Get the report metadata file path"""
        return os.path.join(cls._get_report_folder(report_id), "meta.json")

    @classmethod
    def _get_report_markdown_path(cls, report_id: str) -> str:
        """Get the complete report Markdown file path"""
        return os.path.join(cls._get_report_folder(report_id), "full_report.md")

    @classmethod
    def _get_outline_path(cls, report_id: str) -> str:
        """Get the outline file path"""
        return os.path.join(cls._get_report_folder(report_id), "outline.json")

    @classmethod
    def _get_progress_path(cls, report_id: str) -> str:
        """Get the progress file path"""
        return os.path.join(cls._get_report_folder(report_id), "progress.json")

    @classmethod
    def _get_section_path(cls, report_id: str, section_index: int) -> str:
        """Get the section Markdown file path"""
        return os.path.join(cls._get_report_folder(report_id), f"section_{section_index:02d}.md")

    @classmethod
    def _get_agent_log_path(cls, report_id: str) -> str:
        """Get the Agent log file path"""
        return os.path.join(cls._get_report_folder(report_id), "agent_log.jsonl")

    @classmethod
    def _get_console_log_path(cls, report_id: str) -> str:
        """Get the console log file path"""
        return os.path.join(cls._get_report_folder(report_id), "console_log.txt")
    
    @classmethod
    def get_console_log(cls, report_id: str, from_line: int = 0) -> Dict[str, Any]:
        """
        Get the console log content

        This is the console output log (INFO, WARNING, etc.) produced during
        report generation, distinct from the structured agent_log.jsonl.

        Args:
            report_id: report ID
            from_line: which line to start reading from (for incremental fetching; 0 means from the beginning)

        Returns:
            {
                "logs": [list of log lines],
                "total_lines": total number of lines,
                "from_line": starting line number,
                "has_more": whether there are more logs
            }
        """
        log_path = cls._get_console_log_path(report_id)
        
        if not os.path.exists(log_path):
            return {
                "logs": [],
                "total_lines": 0,
                "from_line": 0,
                "has_more": False
            }
        
        logs = []
        total_lines = 0
        
        with open(log_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                total_lines = i + 1
                if i >= from_line:
                    # Keep the original log line, stripping the trailing newline
                    logs.append(line.rstrip('\n\r'))

        return {
            "logs": logs,
            "total_lines": total_lines,
            "from_line": from_line,
            "has_more": False  # Read to the end
        }

    @classmethod
    def get_console_log_stream(cls, report_id: str) -> List[str]:
        """
        Get the complete console log (fetch everything at once)

        Args:
            report_id: report ID

        Returns:
            A list of log lines
        """
        result = cls.get_console_log(report_id, from_line=0)
        return result["logs"]
    
    @classmethod
    def get_agent_log(cls, report_id: str, from_line: int = 0) -> Dict[str, Any]:
        """
        Get the Agent log content

        Args:
            report_id: report ID
            from_line: which line to start reading from (for incremental fetching; 0 means from the beginning)

        Returns:
            {
                "logs": [list of log entries],
                "total_lines": total number of lines,
                "from_line": starting line number,
                "has_more": whether there are more logs
            }
        """
        log_path = cls._get_agent_log_path(report_id)
        
        if not os.path.exists(log_path):
            return {
                "logs": [],
                "total_lines": 0,
                "from_line": 0,
                "has_more": False
            }
        
        logs = []
        total_lines = 0
        
        with open(log_path, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                total_lines = i + 1
                if i >= from_line:
                    try:
                        log_entry = json.loads(line.strip())
                        logs.append(log_entry)
                    except json.JSONDecodeError:
                        # Skip lines that fail to parse
                        continue

        return {
            "logs": logs,
            "total_lines": total_lines,
            "from_line": from_line,
            "has_more": False  # Read to the end
        }

    @classmethod
    def get_agent_log_stream(cls, report_id: str) -> List[Dict[str, Any]]:
        """
        Get the complete Agent log (for fetching everything at once)

        Args:
            report_id: report ID

        Returns:
            A list of log entries
        """
        result = cls.get_agent_log(report_id, from_line=0)
        return result["logs"]
    
    @classmethod
    def save_outline(cls, report_id: str, outline: ReportOutline) -> None:
        """
        Save the report outline

        Called immediately after the planning stage completes
        """
        cls._ensure_report_folder(report_id)

        with open(cls._get_outline_path(report_id), 'w', encoding='utf-8') as f:
            json.dump(outline.to_dict(), f, ensure_ascii=False, indent=2)

        logger.info(f"Outline saved: {report_id}")
    
    @classmethod
    def save_section(
        cls,
        report_id: str,
        section_index: int,
        section: ReportSection
    ) -> str:
        """
        Save a single section

        Called immediately after each section is generated, to implement section-by-section output

        Args:
            report_id: report ID
            section_index: section index (starting from 1)
            section: the section object

        Returns:
            The path of the saved file
        """
        cls._ensure_report_folder(report_id)

        # Build the section Markdown content - clean up any duplicate headings
        cleaned_content = cls._clean_section_content(section.content, section.title)
        md_content = f"## {section.title}\n\n"
        if cleaned_content:
            md_content += f"{cleaned_content}\n\n"

        # Save the file
        file_suffix = f"section_{section_index:02d}.md"
        file_path = os.path.join(cls._get_report_folder(report_id), file_suffix)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(md_content)

        logger.info(f"Section saved: {report_id}/{file_suffix}")
        return file_path
    
    @classmethod
    def _clean_section_content(cls, content: str, section_title: str) -> str:
        """
        Clean the section content

        1. Remove the Markdown heading line at the start of the content that duplicates the section title
        2. Convert all headings of level ### and below to bold text

        Args:
            content: the original content
            section_title: the section title

        Returns:
            The cleaned content
        """
        import re

        if not content:
            return content

        content = content.strip()
        lines = content.split('\n')
        cleaned_lines = []
        skip_next_empty = False

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Check whether this is a Markdown heading line
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)

            if heading_match:
                level = len(heading_match.group(1))
                title_text = heading_match.group(2).strip()

                # Check whether it duplicates the section title (skip duplicates within the first 5 lines)
                if i < 5:
                    if title_text == section_title or title_text.replace(' ', '') == section_title.replace(' ', ''):
                        skip_next_empty = True
                        continue

                # Convert headings of all levels (#, ##, ###, ####, etc.) to bold
                # Because the section title is added by the system, the content should not contain any heading
                cleaned_lines.append(f"**{title_text}**")
                cleaned_lines.append("")  # Add a blank line
                continue

            # If the previous line was a skipped heading and the current line is empty, skip it too
            if skip_next_empty and stripped == '':
                skip_next_empty = False
                continue

            skip_next_empty = False
            cleaned_lines.append(line)

        # Remove leading blank lines
        while cleaned_lines and cleaned_lines[0].strip() == '':
            cleaned_lines.pop(0)

        # Remove a leading horizontal rule
        while cleaned_lines and cleaned_lines[0].strip() in ['---', '***', '___']:
            cleaned_lines.pop(0)
            # Also remove the blank lines after the rule
            while cleaned_lines and cleaned_lines[0].strip() == '':
                cleaned_lines.pop(0)

        return '\n'.join(cleaned_lines)
    
    @classmethod
    def update_progress(
        cls, 
        report_id: str, 
        status: str, 
        progress: int, 
        message: str,
        current_section: str = None,
        completed_sections: List[str] = None
    ) -> None:
        """
        Update the report generation progress

        Writes progress.json (served back via the report status API) for
        real-time progress polling.
        """
        cls._ensure_report_folder(report_id)
        
        progress_data = {
            "status": status,
            "progress": progress,
            "message": message,
            "current_section": current_section,
            "completed_sections": completed_sections or [],
            "updated_at": datetime.now().isoformat()
        }
        
        with open(cls._get_progress_path(report_id), 'w', encoding='utf-8') as f:
            json.dump(progress_data, f, ensure_ascii=False, indent=2)
    
    @classmethod
    def get_progress(cls, report_id: str) -> Optional[Dict[str, Any]]:
        """Get the report generation progress"""
        path = cls._get_progress_path(report_id)
        
        if not os.path.exists(path):
            return None
        
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    @classmethod
    def get_generated_sections(cls, report_id: str) -> List[Dict[str, Any]]:
        """
        Get the list of already-generated sections

        Returns information about all saved section files
        """
        folder = cls._get_report_folder(report_id)
        
        if not os.path.exists(folder):
            return []
        
        sections = []
        for filename in sorted(os.listdir(folder)):
            if filename.startswith('section_') and filename.endswith('.md'):
                file_path = os.path.join(folder, filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Parse the section index from the filename
                parts = filename.replace('.md', '').split('_')
                section_index = int(parts[1])

                sections.append({
                    "filename": filename,
                    "section_index": section_index,
                    "content": content
                })

        return sections
    
    @classmethod
    def assemble_full_report(cls, report_id: str, outline: ReportOutline) -> str:
        """
        Assemble the complete report

        Assemble the complete report from the saved section files, and perform heading cleanup
        """
        folder = cls._get_report_folder(report_id)

        # Build the report header
        md_content = f"# {outline.title}\n\n"
        md_content += f"> {outline.summary}\n\n"
        md_content += f"---\n\n"

        # Read all section files in order
        sections = cls.get_generated_sections(report_id)
        for section_info in sections:
            md_content += section_info["content"]

        # Post-processing: clean up heading issues across the whole report
        md_content = cls._post_process_report(md_content, outline)

        # Save the complete report
        full_path = cls._get_report_markdown_path(report_id)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(md_content)

        logger.info(f"Complete report assembled: {report_id}")
        return md_content
    
    @classmethod
    def _post_process_report(cls, content: str, outline: ReportOutline) -> str:
        """
        Post-process the report content

        1. Remove duplicate headings
        2. Keep the report main title (#) and section titles (##); remove headings of other levels (###, ####, etc.)
        3. Clean up redundant blank lines and horizontal rules

        Args:
            content: the original report content
            outline: the report outline

        Returns:
            The processed content
        """
        import re

        lines = content.split('\n')
        processed_lines = []
        prev_was_heading = False

        # Collect all section titles from the outline
        section_titles = set()
        for section in outline.sections:
            section_titles.add(section.title)

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Check whether this is a heading line
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)

            if heading_match:
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()

                # Check whether it is a duplicate heading (the same heading content within the last 5 lines)
                is_duplicate = False
                for j in range(max(0, len(processed_lines) - 5), len(processed_lines)):
                    prev_line = processed_lines[j].strip()
                    prev_match = re.match(r'^(#{1,6})\s+(.+)$', prev_line)
                    if prev_match:
                        prev_title = prev_match.group(2).strip()
                        if prev_title == title:
                            is_duplicate = True
                            break

                if is_duplicate:
                    # Skip the duplicate heading and the blank lines after it
                    i += 1
                    while i < len(lines) and lines[i].strip() == '':
                        i += 1
                    continue

                # Heading-level handling:
                # - # (level=1) keep only the report main title
                # - ## (level=2) keep section titles
                # - ### and below (level>=3) convert to bold text

                if level == 1:
                    if title == outline.title:
                        # Keep the report main title
                        processed_lines.append(line)
                        prev_was_heading = True
                    elif title in section_titles:
                        # A section title wrongly used #; correct it to ##
                        processed_lines.append(f"## {title}")
                        prev_was_heading = True
                    else:
                        # Other level-1 headings become bold
                        processed_lines.append(f"**{title}**")
                        processed_lines.append("")
                        prev_was_heading = False
                elif level == 2:
                    if title in section_titles or title == outline.title:
                        # Keep section titles
                        processed_lines.append(line)
                        prev_was_heading = True
                    else:
                        # Non-section level-2 headings become bold
                        processed_lines.append(f"**{title}**")
                        processed_lines.append("")
                        prev_was_heading = False
                else:
                    # Headings of level ### and below become bold text
                    processed_lines.append(f"**{title}**")
                    processed_lines.append("")
                    prev_was_heading = False

                i += 1
                continue

            elif stripped == '---' and prev_was_heading:
                # Skip a horizontal rule immediately following a heading
                i += 1
                continue

            elif stripped == '' and prev_was_heading:
                # Keep only one blank line after a heading
                if processed_lines and processed_lines[-1].strip() != '':
                    processed_lines.append(line)
                prev_was_heading = False

            else:
                processed_lines.append(line)
                prev_was_heading = False

            i += 1

        # Clean up consecutive blank lines (keep at most 2)
        result_lines = []
        empty_count = 0
        for line in processed_lines:
            if line.strip() == '':
                empty_count += 1
                if empty_count <= 2:
                    result_lines.append(line)
            else:
                empty_count = 0
                result_lines.append(line)
        
        return '\n'.join(result_lines)
    
    @classmethod
    def save_report(cls, report: Report) -> None:
        """Save the report metadata and the complete report"""
        cls._ensure_report_folder(report.report_id)

        # Save the metadata JSON
        with open(cls._get_report_path(report.report_id), 'w', encoding='utf-8') as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)

        # Save the outline
        if report.outline:
            cls.save_outline(report.report_id, report.outline)

        # Save the complete Markdown report
        if report.markdown_content:
            with open(cls._get_report_markdown_path(report.report_id), 'w', encoding='utf-8') as f:
                f.write(report.markdown_content)

        logger.info(f"Report saved: {report.report_id}")

    @classmethod
    def get_report(cls, report_id: str) -> Optional[Report]:
        """Get a report"""
        path = cls._get_report_path(report_id)

        if not os.path.exists(path):
            # Backward compatibility: check for a file stored directly under the reports directory
            old_path = safe_join(cls.REPORTS_DIR, f"{report_id}.json")
            if os.path.exists(old_path):
                path = old_path
            else:
                return None

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Rebuild the Report object
        outline = None
        if data.get('outline'):
            outline_data = data['outline']
            sections = []
            for s in outline_data.get('sections', []):
                sections.append(ReportSection(
                    title=s['title'],
                    content=s.get('content', '')
                ))
            outline = ReportOutline(
                title=outline_data['title'],
                summary=outline_data['summary'],
                sections=sections
            )
        
        # If markdown_content is empty, try reading from full_report.md
        markdown_content = data.get('markdown_content', '')
        if not markdown_content:
            full_report_path = cls._get_report_markdown_path(report_id)
            if os.path.exists(full_report_path):
                with open(full_report_path, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
        
        return Report(
            report_id=data['report_id'],
            simulation_id=data['simulation_id'],
            graph_id=data['graph_id'],
            simulation_requirement=data['simulation_requirement'],
            status=ReportStatus(data['status']),
            outline=outline,
            markdown_content=markdown_content,
            created_at=data.get('created_at', ''),
            completed_at=data.get('completed_at', ''),
            error=data.get('error')
        )
    
    @classmethod
    def get_report_by_simulation(cls, simulation_id: str) -> Optional[Report]:
        """Get a report by simulation ID"""
        cls._ensure_reports_dir()

        for item in os.listdir(cls.REPORTS_DIR):
            item_path = safe_join(cls.REPORTS_DIR, item)
            # New format: folder
            if os.path.isdir(item_path):
                report = cls.get_report(item)
                if report and report.simulation_id == simulation_id:
                    return report
            # Backward compatibility: JSON file
            elif item.endswith('.json'):
                report_id = item[:-5]
                report = cls.get_report(report_id)
                if report and report.simulation_id == simulation_id:
                    return report

        return None

    @classmethod
    def list_reports(cls, simulation_id: Optional[str] = None, limit: int = 50) -> List[Report]:
        """List reports"""
        cls._ensure_reports_dir()

        reports = []
        for item in os.listdir(cls.REPORTS_DIR):
            item_path = safe_join(cls.REPORTS_DIR, item)
            # New format: folder
            if os.path.isdir(item_path):
                report = cls.get_report(item)
                if report:
                    if simulation_id is None or report.simulation_id == simulation_id:
                        reports.append(report)
            # Backward compatibility: JSON file
            elif item.endswith('.json'):
                report_id = item[:-5]
                report = cls.get_report(report_id)
                if report:
                    if simulation_id is None or report.simulation_id == simulation_id:
                        reports.append(report)

        # Sort by creation time, descending
        reports.sort(key=lambda r: r.created_at, reverse=True)

        return reports[:limit]

    @classmethod
    def delete_report(cls, report_id: str) -> bool:
        """Delete a report (the entire folder)"""
        import shutil

        folder_path = cls._get_report_folder(report_id)

        # New format: delete the entire folder
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            shutil.rmtree(folder_path)
            logger.info(f"Report folder deleted: {report_id}")
            return True

        # Backward compatibility: delete the individual files
        deleted = False
        old_json_path = safe_join(cls.REPORTS_DIR, f"{report_id}.json")
        old_md_path = safe_join(cls.REPORTS_DIR, f"{report_id}.md")
        
        if os.path.exists(old_json_path):
            os.remove(old_json_path)
            deleted = True
        if os.path.exists(old_md_path):
            os.remove(old_md_path)
            deleted = True
        
        return deleted
