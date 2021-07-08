# This code is part of Qiskit.
#
# (C) Copyright IBM 2021.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Align measurement instructions."""

from collections import defaultdict
from typing import List

from qiskit.circuit.delay import Delay
from qiskit.circuit.measure import Measure
from qiskit.dagcircuit import DAGCircuit
from qiskit.transpiler.basepasses import TransformationPass, AnalysisPass
from qiskit.transpiler.exceptions import TranspilerError


class AlignMeasures(TransformationPass):
    """Measurement alignment.

    This is a control electronics aware optimization pass.

    In many quantum computing architectures gates (instructions) are implemented with
    shaped analog stimulus signals. These signals are digitally stored in the
    waveform memory of the control electronics and converted into analog voltage signals
    by electronic components called digital to analog converters (DAC).

    In a typical hardware implementation of superconducting quantum processors,
    a single qubit instruction is implemented by a
    microwave signal with the duration of around several tens of ns with a per-sample
    time resolution of ~0.1-10ns, as reported by ``backend.configuration().dt``.
    In such systems requiring higher DAC bandwidth, control electronics often
    defines a `pulse granularity`, in other words a data chunk, to allow the DAC to
    perform the signal conversion in parallel to gain the bandwidth.

    Measurement alignment is required if a backend only allows triggering ``measure``
    instructions at a certain multiple value of this pulse granularity.
    This value is usually provided by ``backend.configuration().alignment``.

    In Qiskit SDK, the duration of delay can take arbitrary value in units of ``dt``,
    thus circuits involving delays may violate the above alignment constraint (i.e. misalignment).
    This pass shifts measurement instructions to a new time position to fix the misalignment,
    by inserting extra delay right before the measure instructions.
    The input of this pass should be scheduled :class:`~qiskit.dagcircuit.DAGCircuit`,
    thus one should select one of the scheduling passes
    (:class:`~qiskit.transpiler.passes.ALAPSchedule` or
    :class:`~qiskit.trasnpiler.passes.ASAPSchedule`) before calling this.

    Examples:
        We assume executing the following circuit on a backend with ``alignment=16``.

        .. parsed-literal::

                 ┌───┐┌────────────────┐┌─┐
            q_0: ┤ X ├┤ Delay(100[dt]) ├┤M├
                 └───┘└────────────────┘└╥┘
            c: 1/════════════════════════╩═
                                         0

        Note that delay of 100 dt induces a misalignment of 4 dt at the measurement.
        This pass appends an extra 12 dt time shift to the input circuit.

        .. parsed-literal::

                 ┌───┐┌────────────────┐┌─┐
            q_0: ┤ X ├┤ Delay(112[dt]) ├┤M├
                 └───┘└────────────────┘└╥┘
            c: 1/════════════════════════╩═
                                         0

        This pass always inserts a positive delay before measurements
        rather than reducing other delays.

    Notes:
        The Backend may allow users to execute circuits violating the alignment constraint.
        However, it may return meaningless measurement data mainly due to the phase error.
    """

    def __init__(self, alignment: int = 1):
        """Create new pass.

        Args:
            alignment: Integer number representing the minimum time resolution to
                trigger measure instruction in units of ``dt``. This value depends on
                the control electronics of your quantum processor.
        """
        super().__init__()
        self.alignment = alignment

    def run(self, dag: DAGCircuit):
        """Run the measurement alignment pass on `dag`.

        Args:
            dag (DAGCircuit): DAG to be checked.

        Returns:
            DAGCircuit: DAG with consistent timing and op nodes annotated with duration.

        Raises:
            TranspilerError: If circuit is not scheduled.
        """
        time_unit = self.property_set["time_unit"]

        require_validation = True

        if all(delay_node.op.duration % self.alignment == 0 for delay_node in dag.op_nodes(Delay)):
            # delay is the only instruction that can move other instructions
            # to the position which is not multiple of alignment.
            # if all delays are multiple of alignment then we can avoid validation.
            require_validation = False

        if len(dag.op_nodes(Measure)) == 0:
            # if no measurement is involved we don't need to run validation.
            # since this pass assumes backend execution, this is really rare case.
            require_validation = False

        if self.alignment == 1:
            # we can place measure at arbitrary time of dt.
            require_validation = False

        if not require_validation:
            # return input as-is to avoid unnecessary scheduling.
            # because following procedure regenerate new DAGCircuit,
            # we should avoid continuing if not necessary from performance viewpoint.
            return dag

        # if circuit is not yet scheduled, schedule with ALAP method
        if dag.duration is None:
            raise TranspilerError(
                f"This circuit {dag.name} may involve a delay instruction violating the "
                "pulse controller alignment. To adjust instructions to "
                "right timing, you should call one of scheduling passes first. "
                "This is usually done by calling transpiler with scheduling_method='alap'."
            )

        # the following lines are basically copied from ASAPSchedule pass
        #
        # * some validations for non-scheduled nodes are dropped, since we assume scheduled input
        # * pad_with_delay is called only with non-delay node to avoid consecutive delay
        new_dag = dag._copy_circuit_metadata()

        qubit_time_available = defaultdict(int)
        qubit_stop_times = defaultdict(int)

        def pad_with_delays(qubits: List[int], until, unit) -> None:
            """Pad idle time-slots in ``qubits`` with delays in ``unit`` until ``until``."""
            for q in qubits:
                if qubit_stop_times[q] < until:
                    idle_duration = until - qubit_stop_times[q]
                    new_dag.apply_operation_back(Delay(idle_duration, unit), [q])

        for node in dag.topological_op_nodes():
            start_time = max(qubit_time_available[q] for q in node.qargs)

            if isinstance(node.op, Measure):
                if start_time % self.alignment != 0:
                    start_time = ((start_time // self.alignment) + 1) * self.alignment

            if not isinstance(node.op, Delay):
                pad_with_delays(node.qargs, until=start_time, unit=time_unit)
                new_dag.apply_operation_back(node.op, node.qargs, node.cargs)

                stop_time = start_time + node.op.duration
                # update time table
                for q in node.qargs:
                    qubit_time_available[q] = stop_time
                    qubit_stop_times[q] = stop_time
            else:
                stop_time = start_time + node.op.duration
                for q in node.qargs:
                    qubit_time_available[q] = stop_time

        working_qubits = qubit_time_available.keys()
        circuit_duration = max(qubit_time_available[q] for q in working_qubits)
        pad_with_delays(new_dag.qubits, until=circuit_duration, unit=time_unit)

        new_dag.name = dag.name
        new_dag.metadata = dag.metadata

        # set circuit duration and unit to indicate it is scheduled
        new_dag.duration = circuit_duration
        new_dag.unit = time_unit

        return new_dag


class ValidatePulseGates(AnalysisPass):
    """Check custom gate length.

    This is a control electronics aware analysis pass.

    Quantum gates (instructions) are often implemented with shaped analog stimulus signals.
    These signals may be digitally stored in the waveform memory of the control electronics
    and converted into analog voltage signals by electronic components known as
    digital to analog converters (DAC).

    In Qiskit SDK, we can define the pulse-level implementation of custom quantum gate
    instructions, as a `pulse gate
    <https://qiskit.org/documentation/tutorials/circuits_advanced/05_pulse_gates.html>`__,
    thus user gates should satisfy all waveform memory constraints imposed by the backend.

    This pass validates all attached calibration entries and raises ``TranspilerError`` to
    kill the transpilation process if any invalid calibration entry is found.
    This pass saves users from waiting until job execution time to get an invalid pulse error from
    the backend control electronics.
    """

    def __init__(self, alignment: int = 1):
        """Create new pass.

        Args:
            alignment: Integer number representing the minimum time resolution to
                define the pulse gate length in units of ``dt``. This value depends on
                the control electronics of your quantum processor.
        """
        super().__init__()
        self.alignment = alignment

    def run(self, dag: DAGCircuit):
        """Run the measurement alignment pass on `dag`.

        Args:
            dag (DAGCircuit): DAG to be checked.

        Returns:
            DAGCircuit: DAG with consistent timing and op nodes annotated with duration.

        Raises:
            TranspilerError: When pulse gate violate pulse controller alignment.
        """
        if self.alignment == 1:
            # we can define arbitrary length pulse with dt resolution
            return

        for gate, insts in dag.calibrations.items():
            for qubit_param_pair, schedule in insts.items():
                if schedule.duration % self.alignment != 0:
                    raise TranspilerError(
                        f"Pulse gate duration is not multiple of {self.alignment}. "
                        "This pulse cannot be played on the specified backend. "
                        f"Please modify the duration of the custom gate schedule {schedule.name} "
                        f"which is associated with the gate {gate} of qubit {qubit_param_pair[0]}."
                    )
