from typing import Any, Coroutine, List, Literal, Optional, Union, overload

from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorQuery
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionToolParam,
)

from approaches.approach import ThoughtStep
from approaches.chatapproach import ChatApproach
from core.authentication import AuthenticationHelper
from core.modelhelper import get_token_limit


class ChatReadRetrieveReadApproach(ChatApproach):
    """
    A multi-step approach that first uses OpenAI to turn the user's question into a search query,
    then uses Azure AI Search to retrieve relevant documents, and then sends the conversation history,
    original user question, and search results to OpenAI to generate a response.
    """

    def __init__(
        self,
        *,
        search_client: SearchClient,
        auth_helper: AuthenticationHelper,
        openai_client: AsyncOpenAI,
        chatgpt_model: str,
        chatgpt_deployment: Optional[str],  # Not needed for non-Azure OpenAI
        embedding_deployment: Optional[str],  # Not needed for non-Azure OpenAI or for retrieval_mode="text"
        embedding_model: str,
        sourcepage_field: str,
        content_field: str,
        query_language: str,
        query_speller: str,
    ):
        self.search_client = search_client
        self.openai_client = openai_client
        self.auth_helper = auth_helper
        self.chatgpt_model = chatgpt_model
        self.chatgpt_deployment = chatgpt_deployment
        self.embedding_deployment = embedding_deployment
        self.embedding_model = embedding_model
        self.sourcepage_field = sourcepage_field
        self.content_field = content_field
        self.query_language = query_language
        self.query_speller = query_speller
        self.chatgpt_token_limit = get_token_limit(chatgpt_model)

    @property
    def system_message_chat_conversation(self):
        # return """Assistant only returns API calls in the format api.wastemap.earth/v1/. Assistant must use the description of the API endpoints and the user's question to determine which endpoint should be called, and then return the call to that endpoint in the correct format.
        # For tabular information return it as an html table. Do not return markdown format. If the question is not in English, answer in the language used in the question.
        # {follow_up_questions_prompt}
        # {injected_prompt}
        # """

        return """Assistant generates Python code that will be executed directly in order to make plots of data stored in dask dataframes that satisfy user requests or answer their questions. Ensure ALL output is executable Python code, suitable for direct execution. Any human-readable commentary should be formatted as a comment. 
        The entirety of your output will automatically be fed DIRECTLY to exec() in python, so anything you return that is not python code will cause an error. YOUR RESPONSE MUST CONTAIN ONLY PYTHON CODE. DO NOT EXPLAIN OR DISCUSS IT.
        The first line of your response MUST ALWAYS BE import seaborn as sns.
        The code should focus on data visualization using libraries like Seaborn, with a preference for attractive, publication-quality plots. Do not use more than 20 labels on the x axis, so they are readable and not overcrowded.

        If the code does not successfully run, the error messages will be returned to the assistant. The assistant should then try to correct the errors and return better code. Do not return identical code to your previous attempt!

        Assistant's code will have access to two dataframes, one with the variable name 'country_emissions_by_sector' and one called 'sources'. 

        The columns of the 'country_emissions_by_sector' dask dataframe are: 'iso3_country', 'original_inventory_sector', 'start_time', 'end_time', 'gas', 'emissions_quantity', 'emissions_quantity_units', 'temporal_granularity', 'created_date', 'modified_date', and 'category'. 
        'emissions_quantity' is the primary datapoint, it shows the amount of emissions. 'gas' shows what type of gas is being emitted: the possible values are ch4, co2, co2e_100yr, co2e_20yr. Start time and end time are used to indicate which year the emissions datapoint represents.
        Each row represents emission of a single gas in a single year in a single subsector of a single country. The subsectors, given in the 'original_inventory_sector' column, are 'synthetic-fertilizer-application', 'cropland-fires', 'other-agricultural-soil-emissions', 'manure-management-other', 'enteric-fermentation-other', 'enteric-fermentation-cattle-feedlot', 'manure-left-on-pasture-cattle', 'manure-management-cattle-feedlot', 'enteric-fermentation-cattle-pasture', 'rice-cultivation', 'residential-and-commercial-onsite-fuel-usage', 'other-onsite-fuel-usage', 'fluorinated-gases', 'forest-land-degradation', 'net-shrubgrass', 'forest-land-fires', 'shrubgrass-fires', 'net-wetland', 'net-forest-land', 'water-reservoirs', 'forest-land-clearing', 'removals', 'wetland-fires', 'coal-mining', 'oil-and-gas-production-and-transport', 'other-fossil-fuel-operations', 'oil-and-gas-refining', 'solid-fuel-transformation', 'cement', 'petrochemicals', 'pulp-and-paper', 'other-manufacturing', 'steel', 'chemicals', 'aluminum', 'rock-quarrying', 'iron-mining', 'sand-quarrying', 'copper-mining', 'bauxite-mining', 'electricity-generation', 'other-energy-use', 'domestic-shipping', 'international-aviation', 'railways', 'international-shipping', 'other-transport', 'domestic-aviation', 'road-transportation', 'biological-treatment-of-solid-waste-and-biogenic', 'solid-waste-disposal', 'wastewater-treatment-and-discharge', 'incineration-and-open-burning-of-waste'.
        These sectors and subsectors are from the IPCC. They are subsectors of larger groups; for example, waste emissions come from the 'biological-treatment-of-solid-waste-and-biogenic', 'solid-waste-disposal', 'wastewater-treatment-and-discharge', 'incineration-and-open-burning-of-waste' sectors, so you should sum those when asked about waste emissions. You are familiar with IPCC sectors, use that knowledge. The sector for a row is given in the 'category' column, with possible values 'power', 'waste', 'agriculture', 'transportation', 'mineral_extraction', 'manufacturing', 'fossil_fuel_operations', 'fluorinated_gases', 'forest_and_land_use', and 'buildings'.
        When asked generically about emissions, you should return the 'gas' == 'co2e_100yr' rows.

        The 'sources' df has information on individual emitting sites/pieces of infrastructure like landfills, refineries, etc ('country_emissions_by_sector' only has national-level data). The column headings are 'source_id', 'source_name', 'source_type', 'iso3_country', 'original_inventory_sector', 'start_time', 'end_time', 'lat', 'lon', 'geometry_ref', 'gas', 'emissions_quantity', 'temporal_granularity', 'activity', 'activity_units', 'emissions_factor', 'emissions_factor_units', 'capacity', 'capacity_units', 'capacity_factor', and 'category'. The 'category' column has the same values as in the 'country_emissions_by_sector' dask df.
        Possible values of the 'gas' and 'original_inventory_sector' columns are the same as in the 'country_emissions_by_sector' df.

        Assistant should assume that both 'country_emissions_by_sector' and 'sources' might contain nans. Both files have many millions of rows, so you MUST use dask efficiently. 'sources' is loaded  with sources = dd.read_csv('emissions_sources.csv'), 'country_emissions_by_sector' is loaded with country_emissions_by_sector = dd.read_csv('country_emissions_by_sector.csv'). Remember that dask dataframes are lazy, and plotting packages like matplotlib and seaborn are not dask-aware, they require data in numpy or pandas types/formats. 

        The entirety of your output will automatically be fed DIRECTLY to exec() in python, so anything you return that is not python code will cause an error. YOUR RESPONSE MUST CONTAIN ONLY PYTHON CODE. DO NOT EXPLAIN OR DISCUSS IT.

        {follow_up_questions_prompt}
        {injected_prompt}
        """
    @overload
    async def run_until_final_call(
        self,
        history: list[dict[str, str]],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        should_stream: Literal[False],
    ) -> tuple[dict[str, Any], Coroutine[Any, Any, ChatCompletion]]: ...

    @overload
    async def run_until_final_call(
        self,
        history: list[dict[str, str]],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        should_stream: Literal[True],
    ) -> tuple[dict[str, Any], Coroutine[Any, Any, AsyncStream[ChatCompletionChunk]]]: ...

    async def run_until_final_call(
        self,
        history: list[dict[str, str]],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        should_stream: bool = False,
    ) -> tuple[dict[str, Any], Coroutine[Any, Any, Union[ChatCompletion, AsyncStream[ChatCompletionChunk]]]]:
        has_text = overrides.get("retrieval_mode") in ["text", "hybrid", None]
        has_vector = overrides.get("retrieval_mode") in ["vectors", "hybrid", None]
        use_semantic_captions = True if overrides.get("semantic_captions") and has_text else False
        top = overrides.get("top", 3)
        minimum_search_score = overrides.get("minimum_search_score", 0.0)
        minimum_reranker_score = overrides.get("minimum_reranker_score", 0.0)

        filter = self.build_filter(overrides, auth_claims)
        use_semantic_ranker = True if overrides.get("semantic_ranker") and has_text else False

        original_user_query = history[-1]["content"]
        user_query_request = "Generate search query for: " + original_user_query

        tools: List[ChatCompletionToolParam] = [
            {
                "type": "function",
                "function": {
                    "name": "search_sources",
                    "description": "Retrieve sources from the Azure AI Search index",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "search_query": {
                                "type": "string",
                                "description": "Query string to retrieve documents from azure search eg: 'Health care plan'",
                            }
                        },
                        "required": ["search_query"],
                    },
                },
            }
        ]

        # STEP 1: Generate an optimized keyword search query based on the chat history and the last question
        # query_messages = self.get_messages_from_history(
        #     system_prompt=self.query_prompt_template,
        #     model_id=self.chatgpt_model,
        #     history=history,
        #     user_content=user_query_request,
        #     max_tokens=self.chatgpt_token_limit - len(user_query_request),
        #     few_shots=self.query_prompt_few_shots,
        # )

        # chat_completion: ChatCompletion = await self.openai_client.chat.completions.create(
        #     messages=query_messages,  # type: ignore
        #     # Azure OpenAI takes the deployment name as the model name
        #     model=self.chatgpt_deployment if self.chatgpt_deployment else self.chatgpt_model,
        #     temperature=0.0,  # Minimize creativity for search query generation
        #     max_tokens=100,  # Setting too low risks malformed JSON, setting too high may affect performance
        #     n=1,
        #     tools=tools,
        #     tool_choice="auto",
        # )

        # query_text = self.get_search_query(chat_completion, original_user_query)

        # STEP 2: Retrieve relevant documents from the search index with the GPT optimized query

        # If retrieval mode includes vectors, compute an embedding for the query
        # vectors: list[VectorQuery] = []
        # if has_vector:
        #     vectors.append(await self.compute_text_embedding(query_text))

        # # Only keep the text query if the retrieval mode uses text, otherwise drop it
        # if not has_text:
        #     query_text = None

        # results = await self.search(
        #     top,
        #     query_text,
        #     filter,
        #     vectors,
        #     use_semantic_ranker,
        #     use_semantic_captions,
        #     minimum_search_score,
        #     minimum_reranker_score,
        # )

        # sources_content = self.get_sources_content(results, use_semantic_captions, use_image_citation=False)
        # content = "\n".join(sources_content)

        # STEP 3: Generate a contextual and content specific answer using the search results and chat history

        # Allow client to replace the entire prompt, or to inject into the exiting prompt using >>>
        system_message = self.get_system_prompt(
            overrides.get("prompt_template"),
            self.follow_up_questions_prompt_content if overrides.get("suggest_followup_questions") else "",
        )

        response_token_limit = 1024
        messages_token_limit = self.chatgpt_token_limit - response_token_limit
        messages = self.get_messages_from_history(
            system_prompt=system_message,
            model_id=self.chatgpt_model,
            history=history,
            # Model does not handle lengthy system messages well. Moving sources to latest user conversation to solve follow up questions prompt.
            user_content=original_user_query, # + "\n\nSources:\n" + content,
            max_tokens=messages_token_limit
        )

        # data_points = {"text": sources_content}

        extra_info = {
            # "data_points": data_points,
            "thoughts": [
                # ThoughtStep(
                #     "Prompt to generate search query",
                #     [str(message) for message in query_messages],
                #     (
                #         {"model": self.chatgpt_model, "deployment": self.chatgpt_deployment}
                #         if self.chatgpt_deployment
                #         else {"model": self.chatgpt_model}
                #     ),
                # ),
                # ThoughtStep(
                #     "Search using generated search query",
                #     query_text,
                #     {
                #         "use_semantic_captions": use_semantic_captions,
                #         "use_semantic_ranker": use_semantic_ranker,
                #         "top": top,
                #         "filter": filter,
                #         "has_vector": has_vector,
                #     },
                # ),
                # ThoughtStep(
                #     "Search results",
                #     [result.serialize_for_results() for result in results],
                # ),
                ThoughtStep(
                    "Prompt to generate answer",
                    [str(message) for message in messages],
                    (
                        {"model": self.chatgpt_model, "deployment": self.chatgpt_deployment}
                        if self.chatgpt_deployment
                        else {"model": self.chatgpt_model}
                    ),
                ),
            ],
        }

        chat_coroutine = self.openai_client.chat.completions.create(
            # Azure OpenAI takes the deployment name as the model name
            model=self.chatgpt_deployment if self.chatgpt_deployment else self.chatgpt_model,
            messages=messages,
            temperature=overrides.get("temperature", 0.3),
            max_tokens=response_token_limit,
            n=1,
            stream=should_stream,
        )
        return (extra_info, chat_coroutine)
