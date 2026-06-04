"""
Ontology generation service
Interface 1: Analyze text content and generate the entity and relationship type
definitions suitable for the shopper-psychology simulation.
"""

import json
from typing import Dict, Any, List, Optional
from ..utils.llm_client import LLMClient


# System prompt for ontology generation
ONTOLOGY_SYSTEM_PROMPT = """You are a professional knowledge-graph ontology design expert. Your task is to analyze the given text content and simulation requirement, then design the entity types and relationship types suitable for a **Vakaru Shopify cart-abandonment recovery psychology simulation**.

**Important: You must output valid JSON-formatted data and nothing else. Respond in English.**

## Core task background

We are building a **cart-abandonment recovery psychology simulation system**. In this system:
- Each entity is a "shopper persona" or real-world subject that can react, voice an opinion, raise an objection, and influence a buying decision around an online store, its products, pricing, shipping, trust, and checkout.
- Entities influence one another: they compare notes, reinforce or counter objections, share social proof, and respond to one another.
- We need to simulate how each party reacts in a cart-abandonment scenario and how purchase-intent signals propagate.

Therefore, **entities must be real, concrete subjects that can voice a reaction or play a role in the shopper's decision**:

**Allowed**:
- Specific shopper personas (price-sensitive shopper, comparison shopper, first-time buyer, brand-loyal customer, hesitant browser, gift buyer, deal-hunter, the abandoning customer)
- Companies / brands (including the merchant's own store and its official channels)
- Organizations (review platforms, consumer-advocacy groups, loyalty programs, etc.)
- Payment, shipping, and logistics providers
- Media and review outlets (review sites, influencers, blogs, social channels)
- The Shopify store / checkout itself
- Representatives of a specific group (such as a brand's fan community, a product's existing-owner base, a returns/support team, etc.)

**Not allowed**:
- Abstract concepts (such as "sentiment", "urgency", "trust")
- Topics / themes (such as "price sensitivity", "checkout friction")
- Opinions / stances (such as "the supporters", "the objectors")

## Output format

Output JSON in the following structure:

```json
{
    "entity_types": [
        {
            "name": "Entity type name (English, PascalCase)",
            "description": "Short description (English, no more than 100 characters)",
            "attributes": [
                {
                    "name": "Attribute name (English, snake_case)",
                    "type": "text",
                    "description": "Attribute description"
                }
            ],
            "examples": ["example entity 1", "example entity 2"]
        }
    ],
    "edge_types": [
        {
            "name": "Relationship type name (English, UPPER_SNAKE_CASE)",
            "description": "Short description (English, no more than 100 characters)",
            "source_targets": [
                {"source": "source entity type", "target": "target entity type"}
            ],
            "attributes": []
        }
    ],
    "analysis_summary": "Brief analysis of the text content (English)"
}
```

## Design guidelines (extremely important!)

### 1. Entity type design - must be strictly followed

**Quantity requirement: must be exactly 10 entity types**

**Hierarchy requirement (must include both specific types and fallback types)**:

Your 10 entity types must include the following layers:

A. **Fallback types (must be included, placed as the last 2 in the list)**:
   - `Person`: The fallback type for any individual person. When a person does not fit any more specific person type, classify them here.
   - `Organization`: The fallback type for any organization. When an organization does not fit any more specific organization type, classify it here.

B. **Specific types (8, designed from the text content)**:
   - For the main roles that appear in the text, design more specific types.
   - For example: if the text concerns a fashion store, you might have `PriceSensitiveShopper`, `ComparisonShopper`, `Brand`.
   - For example: if the text concerns an electronics purchase, you might have `FirstTimeBuyer`, `LoyalCustomer`, `PaymentProvider`.

**Why fallback types are needed**:
- The text will contain various people, such as "a casual browser", "a passerby shopper", "some online reviewer".
- If no specific type matches, they should be classified under `Person`.
- Likewise, small businesses, ad-hoc groups, etc. should be classified under `Organization`.

**Design principles for specific types**:
- Identify the high-frequency or key role types from the text.
- Each specific type should have clear boundaries to avoid overlap.
- The description must clearly state how this type differs from the fallback type.

### 2. Relationship type design

- Quantity: 6-10
- Relationships should reflect the real connections in shopper-decision interactions.
- Make sure the relationships' source_targets cover the entity types you defined.

### 3. Attribute design

- 1-3 key attributes per entity type.
- **Note**: attribute names must not use `name`, `uuid`, `group_id`, `created_at`, `summary` (these are system reserved words).
- Recommended: `full_name`, `title`, `role`, `position`, `location`, `description`, etc.

## Entity type reference

**Person class (specific)**:
- PriceSensitiveShopper: a shopper highly focused on price and discounts
- ComparisonShopper: a shopper comparing across stores or products
- FirstTimeBuyer: a first-time buyer unfamiliar with the brand
- LoyalCustomer: a returning, brand-loyal customer
- DealHunter: a shopper waiting for a promotion or coupon
- HesitantBrowser: an undecided browser who stalls at checkout
- GiftBuyer: a shopper purchasing for someone else
- AbandoningCustomer: the customer who abandoned the cart

**Person class (fallback)**:
- Person: any individual (used when none of the specific types above fit)

**Organization class (specific)**:
- Brand: the merchant's brand or store
- Company: a company or business
- PaymentProvider: a payment gateway or method provider
- ShippingProvider: a shipping or logistics provider
- ReviewPlatform: a reviews or ratings platform
- LoyaltyProgram: a rewards or loyalty program
- SupportTeam: the store's customer support / returns team

**Organization class (fallback)**:
- Organization: any organization (used when none of the specific types above fit)

## Relationship type reference

- WORKS_FOR: works for
- SHOPS_AT: shops at
- AFFILIATED_WITH: affiliated with
- REPRESENTS: represents
- REGULATES: regulates
- REPORTS_ON: reports on
- COMMENTS_ON: comments on
- RESPONDS_TO: responds to
- SUPPORTS: supports
- OPPOSES: opposes
- COLLABORATES_WITH: collaborates with
- COMPETES_WITH: competes with
"""


class OntologyGenerator:
    """
    Ontology generator
    Analyzes text content and generates entity and relationship type definitions.
    """
    
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client or LLMClient()
    
    def generate(
        self,
        document_texts: List[str],
        simulation_requirement: str,
        additional_context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate the ontology definition.

        Args:
            document_texts: list of document texts
            simulation_requirement: description of the simulation requirement
            additional_context: additional context

        Returns:
            The ontology definition (entity_types, edge_types, etc.)
        """
        # Build the user message
        user_message = self._build_user_message(
            document_texts,
            simulation_requirement,
            additional_context
        )

        messages = [
            {"role": "system", "content": ONTOLOGY_SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]

        # Call the LLM
        result = self.llm_client.chat_json(
            messages=messages,
            temperature=0.3,
            max_tokens=4096
        )

        # Validate and post-process
        result = self._validate_and_process(result)

        return result

    # Maximum length of text passed to the LLM (50,000 characters)
    MAX_TEXT_LENGTH_FOR_LLM = 50000
    
    def _build_user_message(
        self,
        document_texts: List[str],
        simulation_requirement: str,
        additional_context: Optional[str]
    ) -> str:
        """Build the user message."""

        # Merge the texts
        combined_text = "\n\n---\n\n".join(document_texts)
        original_length = len(combined_text)

        # If the text exceeds 50,000 characters, truncate it (only affects what is
        # sent to the LLM, not graph construction).
        if len(combined_text) > self.MAX_TEXT_LENGTH_FOR_LLM:
            combined_text = combined_text[:self.MAX_TEXT_LENGTH_FOR_LLM]
            combined_text += f"\n\n...(the original text is {original_length} characters; the first {self.MAX_TEXT_LENGTH_FOR_LLM} characters were taken for ontology analysis)..."

        message = f"""## Simulation requirement

{simulation_requirement}

## Document content

{combined_text}
"""

        if additional_context:
            message += f"""
## Additional notes

{additional_context}
"""

        message += """
Based on the content above, design the entity types and relationship types suitable for the cart-abandonment recovery psychology simulation.

**Rules that must be followed**:
1. Must output exactly 10 entity types.
2. The last 2 must be the fallback types: Person (person fallback) and Organization (organization fallback).
3. The first 8 are specific types designed from the text content.
4. Every entity type must be a real-world subject that can voice a reaction, not an abstract concept.
5. Attribute names must not use reserved words like name, uuid, group_id; use full_name, org_name, etc. instead.
"""

        return message
    
    def _validate_and_process(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and post-process the result."""

        # Ensure required fields exist
        if "entity_types" not in result:
            result["entity_types"] = []
        if "edge_types" not in result:
            result["edge_types"] = []
        if "analysis_summary" not in result:
            result["analysis_summary"] = ""
        
        # Validate entity types
        for entity in result["entity_types"]:
            if "attributes" not in entity:
                entity["attributes"] = []
            if "examples" not in entity:
                entity["examples"] = []
            # Ensure description is no more than 100 characters
            if len(entity.get("description", "")) > 100:
                entity["description"] = entity["description"][:97] + "..."

        # Validate relationship types
        for edge in result["edge_types"]:
            if "source_targets" not in edge:
                edge["source_targets"] = []
            if "attributes" not in edge:
                edge["attributes"] = []
            if len(edge.get("description", "")) > 100:
                edge["description"] = edge["description"][:97] + "..."
        
        # Zep API limit: at most 10 custom entity types and at most 10 custom edge types
        MAX_ENTITY_TYPES = 10
        MAX_EDGE_TYPES = 10

        # Fallback type definitions
        person_fallback = {
            "name": "Person",
            "description": "Any individual person not fitting other specific person types.",
            "attributes": [
                {"name": "full_name", "type": "text", "description": "Full name of the person"},
                {"name": "role", "type": "text", "description": "Role or occupation"}
            ],
            "examples": ["casual shopper", "anonymous reviewer"]
        }
        
        organization_fallback = {
            "name": "Organization",
            "description": "Any organization not fitting other specific organization types.",
            "attributes": [
                {"name": "org_name", "type": "text", "description": "Name of the organization"},
                {"name": "org_type", "type": "text", "description": "Type of organization"}
            ],
            "examples": ["small business", "community group"]
        }
        
        # Check whether the fallback types already exist
        entity_names = {e["name"] for e in result["entity_types"]}
        has_person = "Person" in entity_names
        has_organization = "Organization" in entity_names

        # Fallback types that need to be added
        fallbacks_to_add = []
        if not has_person:
            fallbacks_to_add.append(person_fallback)
        if not has_organization:
            fallbacks_to_add.append(organization_fallback)

        if fallbacks_to_add:
            current_count = len(result["entity_types"])
            needed_slots = len(fallbacks_to_add)

            # If adding them would exceed 10, remove some existing types
            if current_count + needed_slots > MAX_ENTITY_TYPES:
                # Compute how many need to be removed
                to_remove = current_count + needed_slots - MAX_ENTITY_TYPES
                # Remove from the end (keep the more important specific types up front)
                result["entity_types"] = result["entity_types"][:-to_remove]

            # Add the fallback types
            result["entity_types"].extend(fallbacks_to_add)

        # Finally, ensure the limit is not exceeded (defensive programming)
        if len(result["entity_types"]) > MAX_ENTITY_TYPES:
            result["entity_types"] = result["entity_types"][:MAX_ENTITY_TYPES]
        
        if len(result["edge_types"]) > MAX_EDGE_TYPES:
            result["edge_types"] = result["edge_types"][:MAX_EDGE_TYPES]
        
        return result
    
    def generate_python_code(self, ontology: Dict[str, Any]) -> str:
        """
        Convert the ontology definition into Python code (similar to ontology.py).

        Args:
            ontology: the ontology definition

        Returns:
            A Python code string.
        """
        code_lines = [
            '"""',
            'Custom entity type definitions',
            'Auto-generated by MiroFish for the cart-abandonment recovery psychology simulation',
            '"""',
            '',
            'from pydantic import Field',
            'from zep_cloud.external_clients.ontology import EntityModel, EntityText, EdgeModel',
            '',
            '',
            '# ============== Entity type definitions ==============',
            '',
        ]

        # Generate entity types
        for entity in ontology.get("entity_types", []):
            name = entity["name"]
            desc = entity.get("description", f"A {name} entity.")
            
            code_lines.append(f'class {name}(EntityModel):')
            code_lines.append(f'    """{desc}"""')
            
            attrs = entity.get("attributes", [])
            if attrs:
                for attr in attrs:
                    attr_name = attr["name"]
                    attr_desc = attr.get("description", attr_name)
                    code_lines.append(f'    {attr_name}: EntityText = Field(')
                    code_lines.append(f'        description="{attr_desc}",')
                    code_lines.append(f'        default=None')
                    code_lines.append(f'    )')
            else:
                code_lines.append('    pass')
            
            code_lines.append('')
            code_lines.append('')
        
        code_lines.append('# ============== Relationship type definitions ==============')
        code_lines.append('')

        # Generate relationship types
        for edge in ontology.get("edge_types", []):
            name = edge["name"]
            # Convert to a PascalCase class name
            class_name = ''.join(word.capitalize() for word in name.split('_'))
            desc = edge.get("description", f"A {name} relationship.")
            
            code_lines.append(f'class {class_name}(EdgeModel):')
            code_lines.append(f'    """{desc}"""')
            
            attrs = edge.get("attributes", [])
            if attrs:
                for attr in attrs:
                    attr_name = attr["name"]
                    attr_desc = attr.get("description", attr_name)
                    code_lines.append(f'    {attr_name}: EntityText = Field(')
                    code_lines.append(f'        description="{attr_desc}",')
                    code_lines.append(f'        default=None')
                    code_lines.append(f'    )')
            else:
                code_lines.append('    pass')
            
            code_lines.append('')
            code_lines.append('')
        
        # Generate the type dictionaries
        code_lines.append('# ============== Type configuration ==============')
        code_lines.append('')
        code_lines.append('ENTITY_TYPES = {')
        for entity in ontology.get("entity_types", []):
            name = entity["name"]
            code_lines.append(f'    "{name}": {name},')
        code_lines.append('}')
        code_lines.append('')
        code_lines.append('EDGE_TYPES = {')
        for edge in ontology.get("edge_types", []):
            name = edge["name"]
            class_name = ''.join(word.capitalize() for word in name.split('_'))
            code_lines.append(f'    "{name}": {class_name},')
        code_lines.append('}')
        code_lines.append('')
        
        # Generate the edge source_targets mapping
        code_lines.append('EDGE_SOURCE_TARGETS = {')
        for edge in ontology.get("edge_types", []):
            name = edge["name"]
            source_targets = edge.get("source_targets", [])
            if source_targets:
                st_list = ', '.join([
                    f'{{"source": "{st.get("source", "Entity")}", "target": "{st.get("target", "Entity")}"}}'
                    for st in source_targets
                ])
                code_lines.append(f'    "{name}": [{st_list}],')
        code_lines.append('}')
        
        return '\n'.join(code_lines)

