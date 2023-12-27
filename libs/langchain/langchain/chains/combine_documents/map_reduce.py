"""Combining documents by mapping a chain over them first, then combining results."""
from __future__ import annotations

from functools import partial
from typing import Any, Dict, List, Optional, Tuple, Type

from langchain_core.documents import Document
from langchain_core.language_models import LanguageModelLike
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import BasePromptTemplate, PromptTemplate
from langchain_core.pydantic_v1 import BaseModel, Extra, create_model, root_validator
from langchain_core.runnables import Runnable, RunnablePassthrough
from langchain_core.runnables.config import RunnableConfig

from langchain.callbacks.manager import Callbacks
from langchain.chains.combine_documents.base import (
    DOCUMENTS_KEY,
    BaseCombineDocumentsChain,
    format_document_inputs,
    format_document_inputs_as_list,
    validate_prompt,
)
from langchain.chains.combine_documents.reduce import ReduceDocumentsChain
from langchain.chains.llm import LLMChain

""" --- LCEL Runnable chains --- """


def create_map_documents_chain(
    llm: LanguageModelLike,
    prompt: BasePromptTemplate,
    *,
    document_prompt: Optional[BasePromptTemplate] = None,
) -> Runnable[Dict[str, Any], List[Document]]:
    """Create a chain that updates the contents of a list of documents by passing them to a model.

    Args:
        llm: Language model to use for mapping document contents.
        prompt: Prompt to use for mapping document contents. Must accept "context" as
            one of the input variables. Each document will be passed in as "context".
        document_prompt: Prompt used for formatting each document into a string. Input
            variables can be "page_content" or any metadata keys that are in all
            documents. "page_content" will automatically retrieve the
            `Document.page_content`, and all other inputs variables will be
            automatically retrieved from the `Document.metadata` dictionary. Default to
            a prompt that only contains `Document.page_content`.

    Returns:
        An LCEL `Runnable` chain.

        Expects a dictionary as input. Input must contain "context" key with a list of
        Documents.

        Returns a list of Documents, with the contents of each document being the output
        of passing the corresponding input document to the model. Document order is
        preserved.

    Example:
        .. code-block:: python

            # pip install -U langchain langchain-community

            from langchain_community.chat_models import ChatOpenAI
            from langchain_core.documents import Document
            from langchain_core.prompts import ChatPromptTemplate
            from langchain.chains.combine_documents import create_map_documents_chain

            llm = ChatOpenAI(model="gpt-3.5-turbo")
            extract_prompt = ChatPromptTemplate.from_template(
                [
                    ("system", "Given a user question, extract the most relevant parts of the following context:\n\n{context}"),
                    ("human", "{question}"),
                ]
            )
            map_documents_chain = create_map_documents_chain(llm, extract_prompt)

            docs = [
                Document(page_content="Jesse loves red but not yellow"),
                Document(page_content = "Jamal loves green but not as much as he loves orange")
            ]

            map_documents_chain.invoke({"context": docs, "question": "Who loves green?"})
    """  # noqa: E501
    validate_prompt(prompt, (DOCUMENTS_KEY,))
    _document_prompt = document_prompt or PromptTemplate.from_template("{page_content}")

    _format = partial(format_document_inputs, document_prompt=_document_prompt)
    map_content_chain = _format | prompt | llm | StrOutputParser()
    map_content_chain = map_content_chain.with_name("map_content")

    assign_page_content: Runnable = RunnablePassthrough.assign(
        page_content=map_content_chain
    )
    assign_page_content = assign_page_content.with_name("assign_page_content")
    map_doc_chain = (assign_page_content | _compile_document).with_name("map_document")

    format_as_list = partial(
        format_document_inputs_as_list, document_prompt=document_prompt
    )
    return (format_as_list | (map_doc_chain.map())).with_name("map_documents_chain")


def create_map_reduce_documents_chain(
    map_documents_chain: Runnable[Dict[str, Any], List[Document]],
    reduce_documents_chain: Runnable[Dict[str, Any], Any],
    *,
    collapse_documents_chain: Optional[Runnable[Dict[str, Any], List[Document]]] = None,
) -> Runnable[Dict[str, Any], Any]:
    """Create a chain that first maps the contents of each document then reduces them.

    Args:
        map_documents_chain: Runnable chain for applying some function to the
            contents of each document. Should accept dictionary as input and output a
            list of Documents.
        reduce_documents_chain: Runnable chain for reducing a list of Documents to a
            single output. Should accept dictionary as input and is expected to read
            the list of Documents from the "context" key.
        collapse_documents_chain: Optional Runnable chain for consolidating a list of
            Documents until the cumulative token size of all Documents is below some
            token limit. Should accept dictionary as input and is expected to read the
            list of Documents from the "context" key. If None, collapse step will not
            be included in final chain. Else will be run after the map_documents_chain
            and before the reduec_documents_chain.

    Returns:
        An LCEL `Runnable` chain.

        Expects a dictionary as input with a list of `Document`s being passed under
        the "context" key.

        Return type matches the reduce_documents_chain return type.

    Example:
        .. code-block:: python

            from langchain_community.chat_models import ChatOpenAI
            from langchain_core.prompts import ChatPromptTemplate
            from langchain.chains.combine_documents import (
                create_collapse_documents_chain,
                create_map_documents_chain,
                create_map_reduce_documents_chain,
                create_stuff_documents_chain,
            )

            llm = ChatOpenAI(model="gpt-3.5-turbo")
            extract_prompt = ChatPromptTemplate.from_template(
                [
                    ("system", "Given a user question, extract the most relevant parts of the following context:\n\n{context}"),
                    ("human", "{question}"),
                ]
            )
            map_documents_chain = create_map_documents_chain(llm, extract_prompt)
            collapse_documents_chain = create_collapse_documents_chain(llm, extract_prompt, token_max=4000)

            answer_prompt = ChatPromptTemplate.from_template(
                [
                    ("system", "Answer the user question using the following context:\n\n{context}"),
                    ("human", "{question}"),
                ]
            )
            reduce_documents_chain = create_stuff_documents_chain(llm, answer_prompt)
            map_reduce_documents_chain = create_map_reduce_documents_chain(
                map_documents_chain,
                reduce_documents_chain,
                collapse_documents_chain=collapse_documents_chain
            )
    """  # noqa: E501
    assign_mapped_docs: Runnable = RunnablePassthrough.assign(
        context=map_documents_chain
    )
    assign_mapped_docs = assign_mapped_docs.with_name("assign_mapped_docs")
    if not collapse_documents_chain:
        return assign_mapped_docs | reduce_documents_chain
    else:
        assign_collapsed_docs: Runnable = RunnablePassthrough.assign(
            context=collapse_documents_chain
        )
        assign_collapsed_docs = assign_collapsed_docs.with_name("assign_collapsed_docs")
        return assign_mapped_docs | assign_collapsed_docs | reduce_documents_chain


""" --- Helper methods for LCEL Runnable chains --- """


def _compile_document(inputs: Dict[str, Any]) -> Document:
    doc = inputs[DOCUMENTS_KEY]
    return Document(page_content=inputs["page_content"], metadata=doc.metadata)


""" --- Legacy Chain --- """


class MapReduceDocumentsChain(BaseCombineDocumentsChain):
    """Combining documents by mapping a chain over them, then combining results.

    We first call `llm_chain` on each document individually, passing in the
    `page_content` and any other kwargs. This is the `map` step.

    We then process the results of that `map` step in a `reduce` step. This should
    likely be a ReduceDocumentsChain.

    Example:
        .. code-block:: python

            from langchain.chains import (
                StuffDocumentsChain,
                LLMChain,
                ReduceDocumentsChain,
                MapReduceDocumentsChain,
            )
            from langchain_core.prompts import PromptTemplate
            from langchain.llms import OpenAI

            # This controls how each document will be formatted. Specifically,
            # it will be passed to `format_document` - see that function for more
            # details.
            document_prompt = PromptTemplate(
                input_variables=["page_content"],
                 template="{page_content}"
            )
            document_variable_name = "context"
            llm = OpenAI()
            # The prompt here should take as an input variable the
            # `document_variable_name`
            prompt = PromptTemplate.from_template(
                "Summarize this content: {context}"
            )
            llm_chain = LLMChain(llm=llm, prompt=prompt)
            # We now define how to combine these summaries
            reduce_prompt = PromptTemplate.from_template(
                "Combine these summaries: {context}"
            )
            reduce_llm_chain = LLMChain(llm=llm, prompt=reduce_prompt)
            combine_documents_chain = StuffDocumentsChain(
                llm_chain=reduce_llm_chain,
                document_prompt=document_prompt,
                document_variable_name=document_variable_name
            )
            reduce_documents_chain = ReduceDocumentsChain(
                combine_documents_chain=combine_documents_chain,
            )
            chain = MapReduceDocumentsChain(
                llm_chain=llm_chain,
                reduce_documents_chain=reduce_documents_chain,
            )
            # If we wanted to, we could also pass in collapse_documents_chain
            # which is specifically aimed at collapsing documents BEFORE
            # the final call.
            prompt = PromptTemplate.from_template(
                "Collapse this content: {context}"
            )
            llm_chain = LLMChain(llm=llm, prompt=prompt)
            collapse_documents_chain = StuffDocumentsChain(
                llm_chain=llm_chain,
                document_prompt=document_prompt,
                document_variable_name=document_variable_name
            )
            reduce_documents_chain = ReduceDocumentsChain(
                combine_documents_chain=combine_documents_chain,
                collapse_documents_chain=collapse_documents_chain,
            )
            chain = MapReduceDocumentsChain(
                llm_chain=llm_chain,
                reduce_documents_chain=reduce_documents_chain,
            )
    """

    llm_chain: LLMChain
    """Chain to apply to each document individually."""
    reduce_documents_chain: BaseCombineDocumentsChain
    """Chain to use to reduce the results of applying `llm_chain` to each doc.
    This typically either a ReduceDocumentChain or StuffDocumentChain."""
    document_variable_name: str
    """The variable name in the llm_chain to put the documents in.
    If only one variable in the llm_chain, this need not be provided."""
    return_intermediate_steps: bool = False
    """Return the results of the map steps in the output."""

    def get_output_schema(
        self, config: Optional[RunnableConfig] = None
    ) -> Type[BaseModel]:
        if self.return_intermediate_steps:
            return create_model(
                "MapReduceDocumentsOutput",
                **{
                    self.output_key: (str, None),
                    "intermediate_steps": (List[str], None),
                },  # type: ignore[call-overload]
            )

        return super().get_output_schema(config)

    @property
    def output_keys(self) -> List[str]:
        """Expect input key.

        :meta private:
        """
        _output_keys = super().output_keys
        if self.return_intermediate_steps:
            _output_keys = _output_keys + ["intermediate_steps"]
        return _output_keys

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True

    @root_validator(pre=True)
    def get_reduce_chain(cls, values: Dict) -> Dict:
        """For backwards compatibility."""
        if "combine_document_chain" in values:
            if "reduce_documents_chain" in values:
                raise ValueError(
                    "Both `reduce_documents_chain` and `combine_document_chain` "
                    "cannot be provided at the same time. `combine_document_chain` "
                    "is deprecated, please only provide `reduce_documents_chain`"
                )
            combine_chain = values["combine_document_chain"]
            collapse_chain = values.get("collapse_document_chain")
            reduce_chain = ReduceDocumentsChain(
                combine_documents_chain=combine_chain,
                collapse_documents_chain=collapse_chain,
            )
            values["reduce_documents_chain"] = reduce_chain
            del values["combine_document_chain"]
            if "collapse_document_chain" in values:
                del values["collapse_document_chain"]

        return values

    @root_validator(pre=True)
    def get_return_intermediate_steps(cls, values: Dict) -> Dict:
        """For backwards compatibility."""
        if "return_map_steps" in values:
            values["return_intermediate_steps"] = values["return_map_steps"]
            del values["return_map_steps"]
        return values

    @root_validator(pre=True)
    def get_default_document_variable_name(cls, values: Dict) -> Dict:
        """Get default document variable name, if not provided."""
        if "document_variable_name" not in values:
            llm_chain_variables = values["llm_chain"].prompt.input_variables
            if len(llm_chain_variables) == 1:
                values["document_variable_name"] = llm_chain_variables[0]
            else:
                raise ValueError(
                    "document_variable_name must be provided if there are "
                    "multiple llm_chain input_variables"
                )
        else:
            llm_chain_variables = values["llm_chain"].prompt.input_variables
            if values["document_variable_name"] not in llm_chain_variables:
                raise ValueError(
                    f"document_variable_name {values['document_variable_name']} was "
                    f"not found in llm_chain input_variables: {llm_chain_variables}"
                )
        return values

    @property
    def collapse_document_chain(self) -> BaseCombineDocumentsChain:
        """Kept for backward compatibility."""
        if isinstance(self.reduce_documents_chain, ReduceDocumentsChain):
            if self.reduce_documents_chain.collapse_documents_chain:
                return self.reduce_documents_chain.collapse_documents_chain
            else:
                return self.reduce_documents_chain.combine_documents_chain
        else:
            raise ValueError(
                f"`reduce_documents_chain` is of type "
                f"{type(self.reduce_documents_chain)} so it does not have "
                f"this attribute."
            )

    @property
    def combine_document_chain(self) -> BaseCombineDocumentsChain:
        """Kept for backward compatibility."""
        if isinstance(self.reduce_documents_chain, ReduceDocumentsChain):
            return self.reduce_documents_chain.combine_documents_chain
        else:
            raise ValueError(
                f"`reduce_documents_chain` is of type "
                f"{type(self.reduce_documents_chain)} so it does not have "
                f"this attribute."
            )

    def combine_docs(
        self,
        docs: List[Document],
        token_max: Optional[int] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Tuple[str, dict]:
        """Combine documents in a map reduce manner.

        Combine by mapping first chain over all documents, then reducing the results.
        This reducing can be done recursively if needed (if there are many documents).
        """
        map_results = self.llm_chain.apply(
            # FYI - this is parallelized and so it is fast.
            [{self.document_variable_name: d.page_content, **kwargs} for d in docs],
            callbacks=callbacks,
        )
        question_result_key = self.llm_chain.output_key
        result_docs = [
            Document(page_content=r[question_result_key], metadata=docs[i].metadata)
            # This uses metadata from the docs, and the textual results from `results`
            for i, r in enumerate(map_results)
        ]
        result, extra_return_dict = self.reduce_documents_chain.combine_docs(
            result_docs, token_max=token_max, callbacks=callbacks, **kwargs
        )
        if self.return_intermediate_steps:
            intermediate_steps = [r[question_result_key] for r in map_results]
            extra_return_dict["intermediate_steps"] = intermediate_steps
        return result, extra_return_dict

    async def acombine_docs(
        self,
        docs: List[Document],
        token_max: Optional[int] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Tuple[str, dict]:
        """Combine documents in a map reduce manner.

        Combine by mapping first chain over all documents, then reducing the results.
        This reducing can be done recursively if needed (if there are many documents).
        """
        map_results = await self.llm_chain.aapply(
            # FYI - this is parallelized and so it is fast.
            [{**{self.document_variable_name: d.page_content}, **kwargs} for d in docs],
            callbacks=callbacks,
        )
        question_result_key = self.llm_chain.output_key
        result_docs = [
            Document(page_content=r[question_result_key], metadata=docs[i].metadata)
            # This uses metadata from the docs, and the textual results from `results`
            for i, r in enumerate(map_results)
        ]
        result, extra_return_dict = await self.reduce_documents_chain.acombine_docs(
            result_docs, token_max=token_max, callbacks=callbacks, **kwargs
        )
        if self.return_intermediate_steps:
            intermediate_steps = [r[question_result_key] for r in map_results]
            extra_return_dict["intermediate_steps"] = intermediate_steps
        return result, extra_return_dict

    @property
    def _chain_type(self) -> str:
        return "map_reduce_documents_chain"
