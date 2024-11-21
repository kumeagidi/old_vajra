from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

from sarathi.config import SystemConfig
from sarathi.core.datatypes.request_output import RequestOutput
from sarathi.core.datatypes.scheduler_output import SchedulerOutput
from sarathi.core.datatypes.sequence import (
    SamplerOutput,
    SamplerOutputs,
    Sequence,
    SequenceMetadata,
    SequenceScheduleMetadata,
)
from sarathi.core.datatypes.sequence_status import SequenceStatus
from sarathi.model_executor.parallel_utils.parallel_state import get_rank
from sarathi.utils.threading_utils import synchronized


class BaseSequenceManager(ABC):

    def __init__(self, config: SystemConfig):
        self.seq_map: Dict[str, Sequence] = {}
        self.enable_sequence_pipeline_parallel = (
            config.parallel_config.enable_sequence_pipeline_parallel
        )
        self.enabled_append_request_execution_stats = (
            config.metrics_config.enabled_append_request_execution_stats
        )

    @synchronized
    def add_seq(self, seq: Sequence) -> None:
        assert seq.seq_id not in self.seq_map
        self.seq_map[seq.seq_id] = seq

    def _free_seq(self, seq_id: str) -> None:
        assert seq_id in self.seq_map
        del self.seq_map[seq_id]

    def _preempt_seq(self, seq_id: str) -> None:
        assert seq_id in self.seq_map
        seq = self.seq_map[seq_id]
        assert seq.is_executing()
        seq.reset_for_recompute()

    def _pause_seq(self, seq_id: str) -> None:
        assert seq_id in self.seq_map
        seq = self.seq_map[seq_id]
        assert seq.is_running(), f"seq_id: {seq_id}, status: {seq.get_status()}"
        seq.set_status(SequenceStatus.PAUSED)

    def _resume_seq(self, seq_id: str) -> None:
        assert seq_id in self.seq_map
        seq = self.seq_map[seq_id]
        assert (
            seq.is_waiting() or seq.is_paused() or seq.is_waiting_preempted()
        ), f"seq_id: {seq_id}, status: {seq.get_status()}"
        seq.set_status(SequenceStatus.RUNNING)

    def _on_seq_scheduled(self, seq_sched_metadata: SequenceScheduleMetadata) -> None:
        assert seq_sched_metadata.seq_id in self.seq_map
        self._resume_seq(seq_sched_metadata.seq_id)

    @abstractmethod
    def _get_block_table(self, seq: Sequence) -> List[int]:
        pass

    @synchronized
    def on_schedule(
        self,
        scheduler_output: SchedulerOutput,
    ) -> Tuple[List[Sequence], List[SequenceMetadata]]:
        ignored_seqs: List[Sequence] = []
        for seq_id in scheduler_output.ignored_seq_ids:
            assert seq_id in self.seq_map
            seq = self.seq_map[seq_id]
            ignored_seqs.append(seq)
            self._free_seq(seq_id)

        for seq_id in scheduler_output.preempted_seq_ids:
            self._preempt_seq(seq_id)

        seq_metadata_list: List[SequenceMetadata] = []

        for seq_sched_metadata in scheduler_output.scheduled_seq_metadata_list:
            self._on_seq_scheduled(seq_sched_metadata)
            seq = self.seq_map[seq_sched_metadata.seq_id]
            seq_metadata_list.append(
                SequenceMetadata(
                    seq_sched_metadata.schedule_id,
                    seq,
                    self._get_block_table(seq),
                    seq_sched_metadata.num_prompt_tokens,
                )
            )

        return ignored_seqs, seq_metadata_list

    @abstractmethod
    def _on_append_token(self, seq: Sequence) -> None:
        pass

    def _process_seq_output(
        self,
        seq: Sequence,
        sample: SamplerOutput,
    ) -> None:
        # at this point, the seq should be in paused state
        assert not seq.is_finished()

        if not seq.prompt_processing_finished:
            return

        # try:
        #     print(
        #         f"rank: {get_rank()} seq_id: {seq.seq_id} output_token: {sample}", flush=True
        #     )
        # except Exception as e:
        #     pass

        seq.append_token_id(sample.output_token)
        self._on_append_token(seq)
        # this function will update the seq status
        # to finished if the stop condition is met
        seq.check_stop()
        if seq.is_finished():
            self._free_seq(seq.seq_id)

    @synchronized
    def on_step_completed(
        self,
        scheduled_seq_metadata_list: List[SequenceScheduleMetadata],
        sampler_outputs: Optional[SamplerOutputs],
    ) -> None:
        for scheduled_seq_metadata, sampler_output in zip(
            scheduled_seq_metadata_list, sampler_outputs
        ):
            assert scheduled_seq_metadata.seq_id == sampler_output.seq_id
            seq = self.seq_map[scheduled_seq_metadata.seq_id]
            if seq.is_waiting_preempted():
                # seq is preempted
                # this can happen with pipeline parallel -- if the system
                # runs out of memory, it will preempt the last arrived request
                # this request might still be executing when the next stage scheduling
                # triggers the preemption
                continue

            if not seq.prompt_processing_finished:
                if not self.enable_sequence_pipeline_parallel:
                    # In case of sequence pipeline parallel, the stage token cursor is
                    # already updated in the on_stage_completed method
                    seq.update_prompt_tokens_stage_processed(
                        scheduled_seq_metadata.prompt_chunk_len
                    )
                seq.update_prompt_tokens_processed(
                    scheduled_seq_metadata.prompt_chunk_len
                )

            if self.enable_sequence_pipeline_parallel:
                if not seq.prompt_stage_processing_finished:
                    # for prompts that are running in sequence parallel manner
                    # they would get unpaused at the end of the stage
                    pass
                elif (
                    seq.prompt_stage_processing_finished
                    and not seq.prompt_processing_finished
                ):
                    # this is the transition phase where the first stage has finished processing the prompt
                    # but there are intermediate micro-batches which are remaining before the prompt processing actually completes
                    pass
                elif seq.prompt_processing_finished:
                    self._pause_seq(scheduled_seq_metadata.seq_id)
            else:
                self._pause_seq(scheduled_seq_metadata.seq_id)

            self._process_seq_output(
                seq,
                sampler_output,
            )

    @synchronized
    def on_stage_completed(
        self,
        scheduler_output: SchedulerOutput,
    ) -> None:
        """
        This gets called only when pipeline parallel is enabled.
        The engine calls this when the first pipeline stage completed (engine-side) + each worker will
        call this method separately.
        """
        if not self.enable_sequence_pipeline_parallel:
            return

        for scheduled_seq_metadata in scheduler_output.scheduled_seq_metadata_list:
            seq = self.seq_map[scheduled_seq_metadata.seq_id]
            assert not seq.is_finished()

            if seq.is_waiting_preempted():
                # seq is preempted
                # this can happen with pipeline parallel -- if the system
                # runs out of memory, it will preempt the last arrived request
                # this request might still be executing when the next stage scheduling
                # triggers the preemption
                continue

            if seq.prompt_stage_processing_finished:
                continue

            seq.update_prompt_tokens_stage_processed(
                scheduled_seq_metadata.prompt_chunk_len
            )
            if not seq.prompt_stage_processing_finished:
                self._pause_seq(scheduled_seq_metadata.seq_id)

    def generate_request_outputs(
        self,
        ignored_seqs: List[Sequence],
        seq_metadata_list: List[SequenceMetadata],
    ) -> List[RequestOutput]:
        all_seqs = ignored_seqs + [x.seq for x in seq_metadata_list]
        return [RequestOutput.from_seq(seq) for seq in all_seqs]
