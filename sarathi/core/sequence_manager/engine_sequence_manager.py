from typing import List, Union

from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast

from sarathi.config import SystemConfig
from sarathi.core.datatypes.sequence import Sequence
from sarathi.core.sequence_manager.base_sequence_manager import BaseSequenceManager
from sarathi.transformers_utils.tokenizer import detokenize_incrementally


class EngineSequenceManager(BaseSequenceManager):

    def __init__(
        self,
        tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast],
        config: SystemConfig,
    ):
        super().__init__(config)
        self.tokenizer = tokenizer

    def _decode_seq(self, seq: Sequence) -> None:
        """Decodes the new token for a sequence."""
        (new_tokens, new_output_text, prefix_offset, read_offset) = (
            detokenize_incrementally(
                self.tokenizer,
                last_five_input_ids=seq.get_last_five_token_ids(),
                prev_tokens=seq.tokens,
                prefix_offset=seq.prefix_offset,
                read_offset=seq.read_offset,
                skip_special_tokens=True,
            )
        )
        if seq.tokens is None:
            seq.tokens = new_tokens
        else:
            seq.tokens.extend(new_tokens)
        seq.prefix_offset = prefix_offset
        seq.read_offset = read_offset
        seq.output_text += new_output_text

    def _on_append_token(self, seq: Sequence) -> None:
        self._decode_seq(seq)

    def _get_block_table(self, seq: Sequence) -> List[int]:
        return []
