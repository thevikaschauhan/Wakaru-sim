"""
OASIS Agent Profile generator
Converts entities from the Zep graph into the Agent Profile format required by
the OASIS simulation platform.

Optimizations:
1. Calls Zep retrieval to further enrich node information
2. Improved prompts that generate very detailed personas
3. Distinguishes individual shopper entities from abstract group entities
"""

import json
import random
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

from zep_cloud.client import Zep

from ..config import Config
from ..utils.logger import get_logger
from ..utils.llm_client import create_chat_completion, LLMClient
from .zep_entity_reader import EntityNode, ZepEntityReader

logger = get_logger('mirofish.oasis_profile')


@dataclass
class OasisAgentProfile:
    """OASIS Agent Profile data structure"""
    # Common fields
    user_id: int
    user_name: str
    name: str
    bio: str
    persona: str

    # Optional fields - Reddit style
    karma: int = 1000

    # Optional fields - Twitter style
    friend_count: int = 100
    follower_count: int = 150
    statuses_count: int = 500

    # Additional persona information
    age: Optional[int] = None
    gender: Optional[str] = None
    mbti: Optional[str] = None
    country: Optional[str] = None
    profession: Optional[str] = None
    interested_topics: List[str] = field(default_factory=list)
    
    # Source entity information
    source_entity_uuid: Optional[str] = None
    source_entity_type: Optional[str] = None

    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))

    def to_reddit_format(self) -> Dict[str, Any]:
        """Convert to Reddit platform format"""
        profile = {
            "user_id": self.user_id,
            "username": self.user_name,  # The OASIS library requires the field name "username" (no underscore)
            "name": self.name,
            "bio": self.bio,
            "persona": self.persona,
            "karma": self.karma,
            "created_at": self.created_at,
        }

        # Add additional persona information (if present)
        if self.age:
            profile["age"] = self.age
        if self.gender:
            profile["gender"] = self.gender
        if self.mbti:
            profile["mbti"] = self.mbti
        if self.country:
            profile["country"] = self.country
        if self.profession:
            profile["profession"] = self.profession
        if self.interested_topics:
            profile["interested_topics"] = self.interested_topics
        
        return profile
    
    def to_twitter_format(self) -> Dict[str, Any]:
        """Convert to Twitter platform format"""
        profile = {
            "user_id": self.user_id,
            "username": self.user_name,  # The OASIS library requires the field name "username" (no underscore)
            "name": self.name,
            "bio": self.bio,
            "persona": self.persona,
            "friend_count": self.friend_count,
            "follower_count": self.follower_count,
            "statuses_count": self.statuses_count,
            "created_at": self.created_at,
        }

        # Add additional persona information
        if self.age:
            profile["age"] = self.age
        if self.gender:
            profile["gender"] = self.gender
        if self.mbti:
            profile["mbti"] = self.mbti
        if self.country:
            profile["country"] = self.country
        if self.profession:
            profile["profession"] = self.profession
        if self.interested_topics:
            profile["interested_topics"] = self.interested_topics
        
        return profile
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to a complete dictionary format"""
        return {
            "user_id": self.user_id,
            "user_name": self.user_name,
            "name": self.name,
            "bio": self.bio,
            "persona": self.persona,
            "karma": self.karma,
            "friend_count": self.friend_count,
            "follower_count": self.follower_count,
            "statuses_count": self.statuses_count,
            "age": self.age,
            "gender": self.gender,
            "mbti": self.mbti,
            "country": self.country,
            "profession": self.profession,
            "interested_topics": self.interested_topics,
            "source_entity_uuid": self.source_entity_uuid,
            "source_entity_type": self.source_entity_type,
            "created_at": self.created_at,
        }


class OasisProfileGenerator:
    """
    OASIS Profile generator

    Converts entities from the Zep graph into the Agent Profiles required by the
    OASIS simulation.

    Optimizations:
    1. Calls Zep graph retrieval to obtain richer context
    2. Generates very detailed personas (basic info, professional background,
       personality traits, shopping/online behavior, etc.)
    3. Distinguishes individual shopper entities from abstract group entities
    """

    # MBTI type list
    MBTI_TYPES = [
        "INTJ", "INTP", "ENTJ", "ENTP",
        "INFJ", "INFP", "ENFJ", "ENFP",
        "ISTJ", "ISFJ", "ESTJ", "ESFJ",
        "ISTP", "ISFP", "ESTP", "ESFP"
    ]
    
    # Common country list
    COUNTRIES = [
        "China", "US", "UK", "Japan", "Germany", "France",
        "Canada", "Australia", "Brazil", "India", "South Korea"
    ]

    # Individual entity types (need a concrete persona generated)
    INDIVIDUAL_ENTITY_TYPES = [
        "student", "alumni", "professor", "person", "publicfigure",
        "expert", "faculty", "official", "journalist", "activist",
        "shopper", "customer", "buyer", "browser", "pricesensitiveshopper",
        "comparisonshopper", "firsttimebuyer", "loyalcustomer", "dealhunter",
        "hesitantbrowser", "giftbuyer", "abandoningcustomer", "visitor"
    ]

    # Group/institutional entity types (need a representative group persona generated)
    GROUP_ENTITY_TYPES = [
        "university", "governmentagency", "organization", "ngo",
        "mediaoutlet", "company", "institution", "group", "community",
        "brand", "store", "merchant", "retailer", "marketplace",
        "paymentprovider", "shippingprovider", "reviewplatform",
        "loyaltyprogram", "supportteam"
    ]
    
    def __init__(
        self, 
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        zep_api_key: Optional[str] = None,
        graph_id: Optional[str] = None,
        llm_client: Optional[LLMClient] = None
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model_name = model_name or Config.LLM_MODEL_NAME

        # All LLM access goes through the shared, timeout'd LLMClient (#22) so the
        # client's timeout and SDK-retry settings live in exactly one place. The
        # persona-generation retry loop below is intentionally kept: it repairs
        # truncated JSON, decays temperature, and falls back to rule-based
        # generation (more than a plain network retry), and it calls
        # create_chat_completion(self.llm.client, ...) directly rather than
        # self.llm.chat(), so it is not double-retried by chat()'s decorator.
        # (LLMClient enforces the LLM_API_KEY check.)
        self.llm = llm_client or LLMClient(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model_name,
        )
        
        # Zep client used to retrieve richer context
        self.zep_api_key = zep_api_key or Config.ZEP_API_KEY
        self.zep_client = None
        self.graph_id = graph_id

        if self.zep_api_key:
            try:
                self.zep_client = Zep(api_key=self.zep_api_key)
            except Exception as e:
                logger.warning(f"Zep client initialization failed: {e}")
    
    def generate_profile_from_entity(
        self,
        entity: EntityNode,
        user_id: int,
        use_llm: bool = True,
        cart_data=None
    ) -> OasisAgentProfile:
        """
        Generate an OASIS Agent Profile from a Zep entity.

        Args:
            entity: Zep entity node
            user_id: User ID (used by OASIS)
            use_llm: Whether to use the LLM to generate a detailed persona
            cart_data: Optional ShopifyCartData with PIE-V2 enrichment fields
                       for context-aware persona tuning.

        Returns:
            OasisAgentProfile
        """
        entity_type = entity.get_entity_type() or "Entity"

        # Basic info
        name = entity.name
        user_name = self._generate_username(name)

        # Build context information
        context = self._build_entity_context(entity)

        # Build tuning context from enriched cart data (PIE-V2)
        tuning_context = self._build_tuning_context(cart_data)

        if use_llm:
            # Use the LLM to generate a detailed persona
            profile_data = self._generate_profile_with_llm(
                entity_name=name,
                entity_type=entity_type,
                entity_summary=entity.summary,
                entity_attributes=entity.attributes,
                context=context,
                tuning_context=tuning_context,
            )
        else:
            # Use rules to generate a basic persona
            profile_data = self._generate_profile_rule_based(
                entity_name=name,
                entity_type=entity_type,
                entity_summary=entity.summary,
                entity_attributes=entity.attributes
            )
        
        return OasisAgentProfile(
            user_id=user_id,
            user_name=user_name,
            name=name,
            bio=profile_data.get("bio", f"{entity_type}: {name}"),
            persona=profile_data.get("persona", entity.summary or f"A {entity_type} named {name}."),
            karma=profile_data.get("karma", random.randint(500, 5000)),
            friend_count=profile_data.get("friend_count", random.randint(50, 500)),
            follower_count=profile_data.get("follower_count", random.randint(100, 1000)),
            statuses_count=profile_data.get("statuses_count", random.randint(100, 2000)),
            age=profile_data.get("age"),
            gender=profile_data.get("gender"),
            mbti=profile_data.get("mbti"),
            country=profile_data.get("country"),
            profession=profile_data.get("profession"),
            interested_topics=profile_data.get("interested_topics", []),
            source_entity_uuid=entity.uuid,
            source_entity_type=entity_type,
        )
    
    def _generate_username(self, name: str) -> str:
        """Generate a username"""
        # Remove special characters and convert to lowercase
        username = name.lower().replace(" ", "_")
        username = ''.join(c for c in username if c.isalnum() or c == '_')

        # Add a random suffix to avoid collisions
        suffix = random.randint(100, 999)
        return f"{username}_{suffix}"
    
    def _search_zep_for_entity(self, entity: EntityNode) -> Dict[str, Any]:
        """
        Use Zep graph hybrid search to fetch rich information about an entity.

        Zep does not provide a built-in hybrid-search endpoint, so we search
        edges and nodes separately and then merge the results. We run the two
        searches in parallel to improve efficiency.

        Args:
            entity: Entity node object

        Returns:
            A dict containing facts, node_summaries, and context
        """
        import concurrent.futures

        if not self.zep_client:
            return {"facts": [], "node_summaries": [], "context": ""}

        entity_name = entity.name

        results = {
            "facts": [],
            "node_summaries": [],
            "context": ""
        }

        # A graph_id is required to perform the search
        if not self.graph_id:
            logger.debug(f"Skipping Zep retrieval: graph_id is not set")
            return results

        comprehensive_query = f"All information, activity, events, relationships, and background about {entity_name}"

        def search_edges():
            """Search edges (facts/relationships) - with retry logic"""
            max_retries = 3
            last_exception = None
            delay = 2.0

            for attempt in range(max_retries):
                try:
                    return self.zep_client.graph.search(
                        query=comprehensive_query,
                        graph_id=self.graph_id,
                        limit=30,
                        scope="edges",
                        reranker="rrf"
                    )
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.debug(f"Zep edge search attempt {attempt + 1} failed: {str(e)[:80]}, retrying...")
                        time.sleep(delay)
                        delay *= 2
                    else:
                        logger.debug(f"Zep edge search still failed after {max_retries} attempts: {e}")
            return None

        def search_nodes():
            """Search nodes (entity summaries) - with retry logic"""
            max_retries = 3
            last_exception = None
            delay = 2.0

            for attempt in range(max_retries):
                try:
                    return self.zep_client.graph.search(
                        query=comprehensive_query,
                        graph_id=self.graph_id,
                        limit=20,
                        scope="nodes",
                        reranker="rrf"
                    )
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.debug(f"Zep node search attempt {attempt + 1} failed: {str(e)[:80]}, retrying...")
                        time.sleep(delay)
                        delay *= 2
                    else:
                        logger.debug(f"Zep node search still failed after {max_retries} attempts: {e}")
            return None

        try:
            # Run edge and node searches in parallel
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                edge_future = executor.submit(search_edges)
                node_future = executor.submit(search_nodes)

                # Collect results
                edge_result = edge_future.result(timeout=30)
                node_result = node_future.result(timeout=30)

            # Process edge search results
            all_facts = set()
            if edge_result and hasattr(edge_result, 'edges') and edge_result.edges:
                for edge in edge_result.edges:
                    if hasattr(edge, 'fact') and edge.fact:
                        all_facts.add(edge.fact)
            results["facts"] = list(all_facts)

            # Process node search results
            all_summaries = set()
            if node_result and hasattr(node_result, 'nodes') and node_result.nodes:
                for node in node_result.nodes:
                    if hasattr(node, 'summary') and node.summary:
                        all_summaries.add(node.summary)
                    if hasattr(node, 'name') and node.name and node.name != entity_name:
                        all_summaries.add(f"Related entity: {node.name}")
            results["node_summaries"] = list(all_summaries)

            # Build the combined context
            context_parts = []
            if results["facts"]:
                context_parts.append("Facts:\n" + "\n".join(f"- {f}" for f in results["facts"][:20]))
            if results["node_summaries"]:
                context_parts.append("Related entities:\n" + "\n".join(f"- {s}" for s in results["node_summaries"][:10]))
            results["context"] = "\n\n".join(context_parts)

            logger.info(f"Zep hybrid retrieval complete: {entity_name}, fetched {len(results['facts'])} facts, {len(results['node_summaries'])} related nodes")

        except concurrent.futures.TimeoutError:
            logger.warning(f"Zep retrieval timed out ({entity_name})")
        except Exception as e:
            logger.warning(f"Zep retrieval failed ({entity_name}): {e}")

        return results
    
    def _build_entity_context(self, entity: EntityNode) -> str:
        """
        Build the complete context information for an entity.

        Includes:
        1. The entity's own edge information (facts)
        2. Detailed information about related nodes
        3. Rich information retrieved via Zep hybrid search
        """
        context_parts = []

        # 1. Add entity attribute information
        if entity.attributes:
            attrs = []
            for key, value in entity.attributes.items():
                if value and str(value).strip():
                    attrs.append(f"- {key}: {value}")
            if attrs:
                context_parts.append("### Entity attributes\n" + "\n".join(attrs))

        # 2. Add related edge information (facts/relationships)
        existing_facts = set()
        if entity.related_edges:
            relationships = []
            for edge in entity.related_edges:  # No count limit
                fact = edge.get("fact", "")
                edge_name = edge.get("edge_name", "")
                direction = edge.get("direction", "")

                if fact:
                    relationships.append(f"- {fact}")
                    existing_facts.add(fact)
                elif edge_name:
                    if direction == "outgoing":
                        relationships.append(f"- {entity.name} --[{edge_name}]--> (related entity)")
                    else:
                        relationships.append(f"- (related entity) --[{edge_name}]--> {entity.name}")

            if relationships:
                context_parts.append("### Related facts and relationships\n" + "\n".join(relationships))

        # 3. Add detailed information about related nodes
        if entity.related_nodes:
            related_info = []
            for node in entity.related_nodes:  # No count limit
                node_name = node.get("name", "")
                node_labels = node.get("labels", [])
                node_summary = node.get("summary", "")

                # Filter out default labels
                custom_labels = [l for l in node_labels if l not in ["Entity", "Node"]]
                label_str = f" ({', '.join(custom_labels)})" if custom_labels else ""

                if node_summary:
                    related_info.append(f"- **{node_name}**{label_str}: {node_summary}")
                else:
                    related_info.append(f"- **{node_name}**{label_str}")

            if related_info:
                context_parts.append("### Related entity information\n" + "\n".join(related_info))

        # 4. Use Zep hybrid search to fetch richer information
        zep_results = self._search_zep_for_entity(entity)

        if zep_results.get("facts"):
            # Deduplicate: exclude facts that already exist
            new_facts = [f for f in zep_results["facts"] if f not in existing_facts]
            if new_facts:
                context_parts.append("### Facts retrieved from Zep\n" + "\n".join(f"- {f}" for f in new_facts[:15]))

        if zep_results.get("node_summaries"):
            context_parts.append("### Related nodes retrieved from Zep\n" + "\n".join(f"- {s}" for s in zep_results["node_summaries"][:10]))

        return "\n\n".join(context_parts)

    def _is_individual_entity(self, entity_type: str) -> bool:
        """Determine whether this is an individual-type entity"""
        type_lower = entity_type.lower()
        if type_lower in self.INDIVIDUAL_ENTITY_TYPES:
            return True
        if type_lower in self.GROUP_ENTITY_TYPES:
            return False
        # Keyword fallback: be robust to LLM type-name variation
        individual_keywords = (
            "shopper", "customer", "buyer", "browser", "visitor",
            "person", "user", "guest", "member", "advocate",
            "reviewer", "influencer"
        )
        if any(kw in type_lower for kw in individual_keywords):
            return True
        group_keywords = (
            "brand", "store", "merchant", "retailer", "marketplace",
            "provider", "platform", "program", "team", "organization",
            "org", "agency", "segment", "network", "institution",
            "community", "group"
        )
        if any(kw in type_lower for kw in group_keywords):
            return False
        # Default: in cart-recovery the common case is an individual shopper
        return True

    def _is_group_entity(self, entity_type: str) -> bool:
        """Determine whether this is a group/institutional-type entity"""
        return entity_type.lower() in self.GROUP_ENTITY_TYPES

    def _build_tuning_context(self, cart_data) -> str:
        """
        Build context-aware tuning instructions from enriched PIE-V2 cart data.

        When cart_data carries recovery history, shopper profile, behavioral
        memory, or merchant effectiveness stats, we inject guidance into the
        persona prompt so the OASIS agents reflect the real shopper context.
        """
        if cart_data is None:
            return ""

        parts: list[str] = []

        # --- Failed recovery angles: de-emphasise corresponding personas ---
        recovery_history = getattr(cart_data, "recovery_history", None) or []
        if recovery_history:
            failed_angles = [
                r.get("angle")
                for r in recovery_history
                if r.get("outcome") in ("ignored", "opened") and r.get("angle")
            ]
            if failed_angles:
                unique = ", ".join(sorted(set(failed_angles)))
                parts.append(
                    f"Note: The following recovery angles have been tried and "
                    f"did NOT convert: {unique}. "
                    f"De-emphasize personas that would recommend these approaches."
                )

        # --- High-value returning customer: amplify Brand Advocate ---
        shopper_profile = getattr(cart_data, "shopper_profile", None)
        if shopper_profile and shopper_profile.get("lifetime_value", 0) > 100:
            parts.append(
                "This is a high-value returning customer. "
                "Amplify the Brand Advocate persona."
            )

        # --- Price sensitivity: amplify Budget Shopper ---
        behavioral_memory = getattr(cart_data, "behavioral_memory", None) or ""
        if behavioral_memory and "price-sensitive" in behavioral_memory.lower():
            parts.append(
                "Behavioral memory indicates price sensitivity. "
                "Amplify the Budget Shopper persona."
            )

        # --- Merchant effectiveness: weight by best-converting angle ---
        merchant_eff = getattr(cart_data, "merchant_effectiveness", None)
        if merchant_eff and merchant_eff.get("top_angle_for_ontology"):
            top_angle = merchant_eff["top_angle_for_ontology"]
            parts.append(
                f"Merchant data shows {top_angle} angle converts best. "
                f"Weight personas accordingly."
            )

        if not parts:
            return ""

        return "\n".join(parts) + "\n"

    def _generate_profile_with_llm(
        self,
        entity_name: str,
        entity_type: str,
        entity_summary: str,
        entity_attributes: Dict[str, Any],
        context: str,
        tuning_context: str = ""
    ) -> Dict[str, Any]:
        """
        Use the LLM to generate a very detailed persona.

        Distinguished by entity type:
        - Individual entity: generate a concrete shopper persona
        - Group/institutional entity: generate a representative group persona
        """

        is_individual = self._is_individual_entity(entity_type)

        if is_individual:
            prompt = self._build_individual_persona_prompt(
                entity_name, entity_type, entity_summary, entity_attributes, context,
                tuning_context=tuning_context,
            )
        else:
            prompt = self._build_group_persona_prompt(
                entity_name, entity_type, entity_summary, entity_attributes, context,
                tuning_context=tuning_context,
            )

        # Try generating multiple times until success or max retries reached
        max_attempts = 3
        last_error = None

        for attempt in range(max_attempts):
            try:
                response = create_chat_completion(
                    self.llm.client,
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": self._get_system_prompt(is_individual)},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.7 - (attempt * 0.1)  # Lower temperature on each retry
                    # Do not set max_tokens; let the LLM produce freely
                )

                content = response.choices[0].message.content

                # Check whether output was truncated (finish_reason is not 'stop')
                finish_reason = response.choices[0].finish_reason
                if finish_reason == 'length':
                    logger.warning(f"LLM output was truncated (attempt {attempt+1}), attempting to repair...")
                    content = self._fix_truncated_json(content)

                # Try to parse the JSON
                try:
                    result = json.loads(content)

                    # Validate required fields
                    if "bio" not in result or not result["bio"]:
                        result["bio"] = entity_summary[:200] if entity_summary else f"{entity_type}: {entity_name}"
                    if "persona" not in result or not result["persona"]:
                        result["persona"] = entity_summary or f"{entity_name} is a {entity_type}."

                    return result

                except json.JSONDecodeError as je:
                    logger.warning(f"JSON parsing failed (attempt {attempt+1}): {str(je)[:80]}")

                    # Try to repair the JSON
                    result = self._try_fix_json(content, entity_name, entity_type, entity_summary)
                    if result.get("_fixed"):
                        del result["_fixed"]
                        return result

                    last_error = je

            except Exception as e:
                logger.warning(f"LLM call failed (attempt {attempt+1}): {str(e)[:80]}")
                last_error = e
                import time
                time.sleep(1 * (attempt + 1))  # Exponential backoff

        logger.warning(f"LLM persona generation failed ({max_attempts} attempts): {last_error}, falling back to rule-based generation")
        return self._generate_profile_rule_based(
            entity_name, entity_type, entity_summary, entity_attributes
        )
    
    def _fix_truncated_json(self, content: str) -> str:
        """Repair truncated JSON (output cut off by the max_tokens limit)"""
        import re

        # If the JSON was truncated, try to close it
        content = content.strip()

        # Count unclosed brackets
        open_braces = content.count('{') - content.count('}')
        open_brackets = content.count('[') - content.count(']')

        # Check for an unclosed string
        # Simple check: if the last quote is not followed by a comma or a
        # closing bracket, the string may have been truncated
        if content and content[-1] not in '",}]':
            # Try to close the string
            content += '"'

        # Close the brackets
        content += ']' * open_brackets
        content += '}' * open_braces

        return content

    def _try_fix_json(self, content: str, entity_name: str, entity_type: str, entity_summary: str = "") -> Dict[str, Any]:
        """Try to repair broken JSON"""
        import re

        # 1. First try to repair the truncated case
        content = self._fix_truncated_json(content)

        # 2. Try to extract the JSON portion
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            json_str = json_match.group()

            # 3. Handle newline issues inside strings
            # Find all string values and replace newlines within them
            def fix_string_newlines(match):
                s = match.group(0)
                # Replace actual newlines inside the string with spaces
                s = s.replace('\n', ' ').replace('\r', ' ')
                # Collapse extra whitespace
                s = re.sub(r'\s+', ' ', s)
                return s

            # Match JSON string values
            json_str = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', fix_string_newlines, json_str)

            # 4. Try to parse
            try:
                result = json.loads(json_str)
                result["_fixed"] = True
                return result
            except json.JSONDecodeError as e:
                # 5. If it still fails, try a more aggressive repair
                try:
                    # Remove all control characters
                    json_str = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', json_str)
                    # Collapse all consecutive whitespace
                    json_str = re.sub(r'\s+', ' ', json_str)
                    result = json.loads(json_str)
                    result["_fixed"] = True
                    return result
                except:
                    pass

        # 6. Try to extract partial information from the content
        bio_match = re.search(r'"bio"\s*:\s*"([^"]*)"', content)
        persona_match = re.search(r'"persona"\s*:\s*"([^"]*)', content)  # May be truncated

        bio = bio_match.group(1) if bio_match else (entity_summary[:200] if entity_summary else f"{entity_type}: {entity_name}")
        persona = persona_match.group(1) if persona_match else (entity_summary or f"{entity_name} is a {entity_type}.")

        # If we extracted meaningful content, mark it as fixed
        if bio_match or persona_match:
            logger.info(f"Extracted partial information from broken JSON")
            return {
                "bio": bio,
                "persona": persona,
                "_fixed": True
            }

        # 7. Complete failure; return a basic structure
        logger.warning(f"JSON repair failed; returning a basic structure")
        return {
            "bio": entity_summary[:200] if entity_summary else f"{entity_type}: {entity_name}",
            "persona": entity_summary or f"{entity_name} is a {entity_type}."
        }
    
    def _get_system_prompt(self, is_individual: bool) -> str:
        """Get the system prompt"""
        base_prompt = "You are an expert at generating shopper personas for Vakaru, a Shopify cart-abandonment recovery engine. Generate detailed, realistic shopper personas for a psychology simulation that predicts why a customer abandoned their cart and how to win them back, staying as faithful as possible to the real situation. You must return valid JSON. No string value may contain unescaped newlines. Respond in English."
        return base_prompt
    
    def _build_individual_persona_prompt(
        self,
        entity_name: str,
        entity_type: str,
        entity_summary: str,
        entity_attributes: Dict[str, Any],
        context: str,
        tuning_context: str = ""
    ) -> str:
        """Build the detailed persona prompt for an individual entity"""

        attrs_str = json.dumps(entity_attributes, ensure_ascii=False) if entity_attributes else "none"
        context_str = context[:3000] if context else "no additional context"

        # Inject tuning context before entity details when available
        tuning_block = ""
        if tuning_context:
            tuning_block = (
                f"--- Recovery context (use to adjust persona emphasis) ---\n"
                f"{tuning_context}"
                f"--- End recovery context ---\n\n"
            )

        return f"""{tuning_block}Generate a detailed shopper persona for this entity, to be used as an agent in a Vakaru cart-abandonment recovery psychology simulation. Stay as faithful as possible to the real situation.

Entity name: {entity_name}
Entity type: {entity_type}
Entity summary: {entity_summary}
Entity attributes: {attrs_str}

Context:
{context_str}

Generate JSON with the following fields:

1. bio: A short shopper bio, around 200 words.
2. persona: A detailed persona description (around 2000 words of plain text) that includes:
   - Basic info (age, occupation, education background, location)
   - Shopper background (relevant purchase history, relationship to this product/store/cart, life context that shapes their buying)
   - Personality traits (MBTI type, core personality, how they express emotion)
   - Shopping and online behavior (how they browse, research, and decide; price sensitivity; brand loyalty; checkout habits; what makes them hesitate)
   - Stance and objections (their attitude toward the product, price, shipping, trust, and checkout; what could push them to abandon or what could win them back)
   - Distinctive traits (catchphrases, notable experiences, personal hobbies)
   - Cart memory (a key part of the persona: describe this shopper's connection to the abandoned cart, what they left behind, their likely emotional state at the moment of abandonment, and any prior signals (e.g. removed items, applied/removed discounts) that hint at their reasoning.)
3. age: Age as a number (must be an integer)
4. gender: Gender, must be in English: "male" or "female"
5. mbti: MBTI type (e.g. INTJ, ENFP)
6. country: Country (e.g. "US", "UK", "Germany" — match the entity's actual location)
7. profession: Occupation
8. interested_topics: An array of topics of interest

Important:
- All field values must be strings or numbers; do not use newline characters.
- persona must be a single coherent block of text.
- Respond in English (the gender field must be "male" or "female").
- Content must stay consistent with the entity information.
- age must be a valid integer, and gender must be "male" or "female".
"""

    def _build_group_persona_prompt(
        self,
        entity_name: str,
        entity_type: str,
        entity_summary: str,
        entity_attributes: Dict[str, Any],
        context: str,
        tuning_context: str = ""
    ) -> str:
        """Build the detailed persona prompt for a group/institutional entity"""

        attrs_str = json.dumps(entity_attributes, ensure_ascii=False) if entity_attributes else "none"
        context_str = context[:3000] if context else "no additional context"

        # Inject tuning context before entity details when available
        tuning_block = ""
        if tuning_context:
            tuning_block = (
                f"--- Recovery context (use to adjust persona emphasis) ---\n"
                f"{tuning_context}"
                f"--- End recovery context ---\n\n"
            )

        return f"""{tuning_block}Generate a detailed persona for this group/institutional entity, to be used as a representative shopper-segment agent in a Vakaru cart-abandonment recovery psychology simulation. Stay as faithful as possible to the real situation.

Entity name: {entity_name}
Entity type: {entity_type}
Entity summary: {entity_summary}
Entity attributes: {attrs_str}

Context:
{context_str}

Generate JSON with the following fields:

1. bio: A short, professional profile of the group/segment, around 200 words.
2. persona: A detailed persona description (around 2000 words of plain text) that includes:
   - Group basic info (formal name, nature of the group/segment, how it came to be, main purpose)
   - Segment positioning (what kind of shoppers it represents, target audience, core motivations)
   - Voice and tone (language style, common expressions, topics it avoids)
   - Behavior patterns (typical shopping behavior, how often the segment buys, when it is most active)
   - Stance and objections (the segment's collective attitude toward the product, price, shipping, trust, and checkout; how it handles hesitation or doubt)
   - Special notes (the shopper profile it represents, its purchasing habits)
   - Cart memory (a key part of the persona: describe this segment's typical relationship to the cart items and the hesitation signals or decision patterns that lead to abandonment.)
3. age: Always set to 30 (the virtual age of a group account)
4. gender: Always set to "other" (a group account uses "other" to indicate it is not an individual)
5. mbti: MBTI type, used to describe the account's style (e.g. ISTJ for rigorous and conservative)
6. country: Country (e.g. "US", "UK", "Germany" — match the entity's actual location)
7. profession: A description of the group's function
8. interested_topics: An array of focus areas

Important:
- All field values must be strings or numbers; null values are not allowed.
- persona must be a single coherent block of text; do not use newline characters.
- Respond in English (the gender field must be "other").
- age must be the integer 30, and gender must be the string "other".
- The group account's voice must match its positioning."""
    
    def _generate_profile_rule_based(
        self,
        entity_name: str,
        entity_type: str,
        entity_summary: str,
        entity_attributes: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Use rules to generate a basic persona"""

        # Generate a different persona depending on the entity type
        entity_type_lower = entity_type.lower()

        if entity_type_lower in ["student", "alumni"]:
            return {
                "bio": f"{entity_type} who is a budget-conscious online shopper.",
                "persona": f"{entity_name} is a {entity_type.lower()} who shops online frequently but watches every dollar. They compare prices, look for discount codes, and often hesitate at checkout when shipping costs appear.",
                "age": random.randint(18, 30),
                "gender": random.choice(["male", "female"]),
                "mbti": random.choice(self.MBTI_TYPES),
                "country": random.choice(self.COUNTRIES),
                "profession": "Student",
                "interested_topics": ["Deals & Discounts", "Online Shopping", "Technology"],
            }

        elif entity_type_lower in ["publicfigure", "expert", "faculty"]:
            return {
                "bio": f"Discerning, brand-aware shopper who values quality and trust.",
                "persona": f"{entity_name} is a {entity_type.lower()} who shops deliberately and expects a polished, trustworthy checkout experience. They are loyal to brands they trust, sensitive to reviews and social proof, and quick to abandon a cart when something feels off.",
                "age": random.randint(35, 60),
                "gender": random.choice(["male", "female"]),
                "mbti": random.choice(["ENTJ", "INTJ", "ENTP", "INTP"]),
                "country": random.choice(self.COUNTRIES),
                "profession": entity_attributes.get("occupation", "Professional"),
                "interested_topics": ["Quality & Value", "Brand Trust", "Premium Products"],
            }

        elif entity_type_lower in ["mediaoutlet", "socialmediaplatform"]:
            return {
                "bio": f"Representative shopper segment associated with {entity_name}.",
                "persona": f"{entity_name} represents a shopper segment that responds strongly to trends, recommendations, and social proof. The segment is influenced by what is popular and abandons carts when products feel unproven or trust signals are missing.",
                "age": 30,  # Virtual age for a group account
                "gender": "other",  # Group accounts use "other"
                "mbti": "ISTJ",  # Group style: rigorous and conservative
                "country": random.choice(self.COUNTRIES),
                "profession": "Trend-driven shopper segment",
                "interested_topics": ["Trending Products", "Social Proof", "Recommendations"],
            }

        elif entity_type_lower in ["university", "governmentagency", "ngo", "organization"]:
            return {
                "bio": f"Representative shopper segment associated with {entity_name}.",
                "persona": f"{entity_name} represents an organized shopper segment with consistent expectations around value, reliability, and a clear, low-friction checkout. The segment abandons carts when pricing or shipping feels unjustified.",
                "age": 30,  # Virtual age for a group account
                "gender": "other",  # Group accounts use "other"
                "mbti": "ISTJ",  # Group style: rigorous and conservative
                "country": random.choice(self.COUNTRIES),
                "profession": entity_type,
                "interested_topics": ["Value & Reliability", "Customer Experience", "Checkout Friction"],
            }

        else:
            # Default persona
            return {
                "bio": entity_summary[:150] if entity_summary else f"{entity_type}: {entity_name}",
                "persona": entity_summary or f"{entity_name} is a {entity_type.lower()} who shops online and is part of this store's potential customer base.",
                "age": random.randint(25, 50),
                "gender": random.choice(["male", "female"]),
                "mbti": random.choice(self.MBTI_TYPES),
                "country": random.choice(self.COUNTRIES),
                "profession": entity_type,
                "interested_topics": ["Online Shopping", "Deals & Discounts"],
            }
    
    def set_graph_id(self, graph_id: str):
        """Set the graph ID used for Zep retrieval"""
        self.graph_id = graph_id
    
    def generate_profiles_from_entities(
        self,
        entities: List[EntityNode],
        use_llm: bool = True,
        progress_callback: Optional[callable] = None,
        graph_id: Optional[str] = None,
        parallel_count: int = 5,
        realtime_output_path: Optional[str] = None,
        output_platform: str = "reddit",
        cart_data=None
    ) -> List[OasisAgentProfile]:
        """
        Generate Agent Profiles from a batch of entities (supports parallel generation).

        Args:
            entities: List of entities
            use_llm: Whether to use the LLM to generate detailed personas
            progress_callback: Progress callback function (current, total, message)
            graph_id: Graph ID, used for Zep retrieval to fetch richer context
            parallel_count: Number of profiles to generate in parallel, default 5
            realtime_output_path: File path to write to in real time (if provided, writes once per generated profile)
            output_platform: Output platform format ("reddit" or "twitter")
            cart_data: Optional ShopifyCartData with PIE-V2 enrichment fields
                       for context-aware persona tuning.

        Returns:
            List of Agent Profiles
        """
        import concurrent.futures
        from threading import Lock

        # Set graph_id for Zep retrieval
        if graph_id:
            self.graph_id = graph_id

        total = len(entities)
        profiles = [None] * total  # Pre-allocate the list to preserve order
        completed_count = [0]  # Use a list so it can be mutated inside the closure
        lock = Lock()

        # Helper for writing the file in real time
        def save_profiles_realtime():
            """Save the already-generated profiles to file in real time"""
            if not realtime_output_path:
                return

            with lock:
                # Filter out the profiles that have been generated
                existing_profiles = [p for p in profiles if p is not None]
                if not existing_profiles:
                    return

                try:
                    if output_platform == "reddit":
                        # Reddit JSON format
                        profiles_data = [p.to_reddit_format() for p in existing_profiles]
                        with open(realtime_output_path, 'w', encoding='utf-8') as f:
                            json.dump(profiles_data, f, ensure_ascii=False, indent=2)
                    else:
                        # Twitter CSV format
                        import csv
                        profiles_data = [p.to_twitter_format() for p in existing_profiles]
                        if profiles_data:
                            fieldnames = list(profiles_data[0].keys())
                            with open(realtime_output_path, 'w', encoding='utf-8', newline='') as f:
                                writer = csv.DictWriter(f, fieldnames=fieldnames)
                                writer.writeheader()
                                writer.writerows(profiles_data)
                except Exception as e:
                    logger.warning(f"Real-time profile save failed: {e}")

        def generate_single_profile(idx: int, entity: EntityNode) -> tuple:
            """Worker function that generates a single profile"""
            entity_type = entity.get_entity_type() or "Entity"

            try:
                profile = self.generate_profile_from_entity(
                    entity=entity,
                    user_id=idx,
                    use_llm=use_llm,
                    cart_data=cart_data,
                )

                # Print the generated persona to the console and log in real time
                self._print_generated_profile(entity.name, entity_type, profile)

                return idx, profile, None

            except Exception as e:
                logger.error(f"Failed to generate persona for entity {entity.name}: {str(e)}")
                # Create a basic profile
                fallback_profile = OasisAgentProfile(
                    user_id=idx,
                    user_name=self._generate_username(entity.name),
                    name=entity.name,
                    bio=f"{entity_type}: {entity.name}",
                    persona=entity.summary or f"A shopper in this store's customer base.",
                    source_entity_uuid=entity.uuid,
                    source_entity_type=entity_type,
                )
                return idx, fallback_profile, str(e)

        logger.info(f"Starting parallel generation of {total} agent personas (parallelism: {parallel_count})...")
        print(f"\n{'='*60}")
        print(f"Generating agent personas - {total} entities total, parallelism: {parallel_count}")
        print(f"{'='*60}\n")

        # Run in parallel using a thread pool
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_count) as executor:
            # Submit all tasks
            future_to_entity = {
                executor.submit(generate_single_profile, idx, entity): (idx, entity)
                for idx, entity in enumerate(entities)
            }

            # Collect results
            for future in concurrent.futures.as_completed(future_to_entity):
                idx, entity = future_to_entity[future]
                entity_type = entity.get_entity_type() or "Entity"

                try:
                    result_idx, profile, error = future.result()
                    profiles[result_idx] = profile

                    with lock:
                        completed_count[0] += 1
                        current = completed_count[0]

                    # Write the file in real time
                    save_profiles_realtime()

                    if progress_callback:
                        progress_callback(
                            current,
                            total,
                            f"Completed {current}/{total}: {entity.name} ({entity_type})"
                        )

                    if error:
                        logger.warning(f"[{current}/{total}] {entity.name} used the fallback persona: {error}")
                    else:
                        logger.info(f"[{current}/{total}] Successfully generated persona: {entity.name} ({entity_type})")

                except Exception as e:
                    logger.error(f"Exception while processing entity {entity.name}: {str(e)}")
                    with lock:
                        completed_count[0] += 1
                    profiles[idx] = OasisAgentProfile(
                        user_id=idx,
                        user_name=self._generate_username(entity.name),
                        name=entity.name,
                        bio=f"{entity_type}: {entity.name}",
                        persona=entity.summary or "A shopper in this store's customer base.",
                        source_entity_uuid=entity.uuid,
                        source_entity_type=entity_type,
                    )
                    # Write the file in real time (even for the fallback persona)
                    save_profiles_realtime()

        print(f"\n{'='*60}")
        print(f"Persona generation complete! Generated {len([p for p in profiles if p])} agents")
        print(f"{'='*60}\n")

        return profiles
    
    def _print_generated_profile(self, entity_name: str, entity_type: str, profile: OasisAgentProfile):
        """Print the generated persona to the console in real time (full content, no truncation)"""
        separator = "-" * 70

        # Build the full output content (no truncation)
        topics_str = ', '.join(profile.interested_topics) if profile.interested_topics else 'none'

        output_lines = [
            f"\n{separator}",
            f"[Generated] {entity_name} ({entity_type})",
            f"{separator}",
            f"Username: {profile.user_name}",
            f"",
            f"[Bio]",
            f"{profile.bio}",
            f"",
            f"[Detailed persona]",
            f"{profile.persona}",
            f"",
            f"[Basic attributes]",
            f"Age: {profile.age} | Gender: {profile.gender} | MBTI: {profile.mbti}",
            f"Profession: {profile.profession} | Country: {profile.country}",
            f"Topics of interest: {topics_str}",
            separator
        ]

        output = "\n".join(output_lines)

        # Print to the console only (avoid duplication; logger no longer prints full content)
        print(output)
    
    def save_profiles(
        self,
        profiles: List[OasisAgentProfile],
        file_path: str,
        platform: str = "reddit"
    ):
        """
        Save profiles to a file (choosing the correct format per platform).

        OASIS platform format requirements:
        - Twitter: CSV format
        - Reddit: JSON format

        Args:
            profiles: List of profiles
            file_path: File path
            platform: Platform type ("reddit" or "twitter")
        """
        if platform == "twitter":
            self._save_twitter_csv(profiles, file_path)
        else:
            self._save_reddit_json(profiles, file_path)

    def _save_twitter_csv(self, profiles: List[OasisAgentProfile], file_path: str):
        """
        Save Twitter profiles in CSV format (conforming to the official OASIS requirements).

        CSV fields required by OASIS Twitter:
        - user_id: User ID (starts at 0 following the CSV order)
        - name: The user's real name
        - username: The username in the system
        - user_char: Detailed persona description (injected into the LLM system prompt to guide agent behavior)
        - description: Short public bio (shown on the user's profile page)

        user_char vs description:
        - user_char: Internal use, LLM system prompt, determines how the agent thinks and acts
        - description: External display, the bio visible to other users
        """
        import csv

        # Ensure the file extension is .csv
        if not file_path.endswith('.csv'):
            file_path = file_path.replace('.json', '.csv')

        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)

            # Write the header required by OASIS
            headers = ['user_id', 'name', 'username', 'user_char', 'description']
            writer.writerow(headers)

            # Write the data rows
            for idx, profile in enumerate(profiles):
                # user_char: full persona (bio + persona), used for the LLM system prompt
                user_char = profile.bio
                if profile.persona and profile.persona != profile.bio:
                    user_char = f"{profile.bio} {profile.persona}"
                # Handle newlines (replace with spaces in CSV)
                user_char = user_char.replace('\n', ' ').replace('\r', ' ')

                # description: short bio, used for external display
                description = profile.bio.replace('\n', ' ').replace('\r', ' ')

                row = [
                    idx,                    # user_id: sequential ID starting at 0
                    profile.name,           # name: real name
                    profile.user_name,      # username: username
                    user_char,              # user_char: full persona (internal LLM use)
                    description             # description: short bio (external display)
                ]
                writer.writerow(row)

        logger.info(f"Saved {len(profiles)} Twitter profiles to {file_path} (OASIS CSV format)")
    
    def _normalize_gender(self, gender: Optional[str]) -> str:
        """
        Normalize the gender field into the English format required by OASIS.

        OASIS requires: male, female, other
        """
        if not gender:
            return "other"

        gender_lower = gender.lower().strip()

        # Accepted English values
        gender_map = {
            "male": "male",
            "female": "female",
            "other": "other",
            # Backward-compat i18n shim: legacy persisted Chinese values
            "男": "male",
            "女": "female",
            "机构": "other",
            "其他": "other",
        }

        return gender_map.get(gender_lower, "other")
    
    def _save_reddit_json(self, profiles: List[OasisAgentProfile], file_path: str):
        """
        Save Reddit profiles in JSON format.

        Uses a format consistent with to_reddit_format() to ensure OASIS can
        read it correctly. The user_id field is required - it is the key that
        OASIS agent_graph.get_agent() matches on!

        Required fields:
        - user_id: User ID (integer, used to match poster_agent_id in initial_posts)
        - username: Username
        - name: Display name
        - bio: Bio
        - persona: Detailed persona
        - age: Age (integer)
        - gender: "male", "female", or "other"
        - mbti: MBTI type
        - country: Country
        """
        data = []
        for idx, profile in enumerate(profiles):
            # Use a format consistent with to_reddit_format()
            item = {
                "user_id": profile.user_id if profile.user_id is not None else idx,  # Key: user_id must be present
                "username": profile.user_name,
                "name": profile.name,
                "bio": profile.bio[:150] if profile.bio else f"{profile.name}",
                "persona": profile.persona or f"{profile.name} is a shopper in this store's customer base.",
                "karma": profile.karma if profile.karma else 1000,
                "created_at": profile.created_at,
                # OASIS required fields - ensure each has a default value
                "age": profile.age if profile.age else 30,
                "gender": self._normalize_gender(profile.gender),
                "mbti": profile.mbti if profile.mbti else "ISTJ",
                "country": profile.country if profile.country else "US",
            }

            # Optional fields
            if profile.profession:
                item["profession"] = profile.profession
            if profile.interested_topics:
                item["interested_topics"] = profile.interested_topics

            data.append(item)

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"Saved {len(profiles)} Reddit profiles to {file_path} (JSON format, includes the user_id field)")

    # Keep the old method name as an alias for backward compatibility
    def save_profiles_to_json(
        self,
        profiles: List[OasisAgentProfile],
        file_path: str,
        platform: str = "reddit"
    ):
        """[Deprecated] Please use the save_profiles() method"""
        logger.warning("save_profiles_to_json is deprecated; please use the save_profiles method")
        self.save_profiles(profiles, file_path, platform)

