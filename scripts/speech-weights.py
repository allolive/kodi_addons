"""The 16 kHz weights of Silero VAD v5, out of its ONNX file, as float32 little endian in the
order Kodi's CSpeechDetector reads them.
usage: speech-weights.py <silero_vad.onnx> <out.bin>"""
import sys

import numpy as np
import onnx
from onnx import numpy_helper

ORDER = ["stft.forward_basis_buffer",
         "encoder.0.reparam_conv.weight", "encoder.0.reparam_conv.bias",
         "encoder.1.reparam_conv.weight", "encoder.1.reparam_conv.bias",
         "encoder.2.reparam_conv.weight", "encoder.2.reparam_conv.bias",
         "encoder.3.reparam_conv.weight", "encoder.3.reparam_conv.bias",
         "decoder.rnn.weight_ih", "decoder.rnn.weight_hh",
         "decoder.rnn.bias_ih", "decoder.rnn.bias_hh",
         "decoder.decoder.2.weight", "decoder.decoder.2.bias"]

model = onnx.load(sys.argv[1])
# the graph branches on the sample rate; the branch taken at 16 kHz holds its weights as constants
branch = next(n for n in model.graph.node if n.op_type == "If")
then = next(a.g for a in branch.attribute if a.name == "then_branch")
weights = {}
for node in then.node:
    if node.op_type == "Constant":
        name = node.output[0].split("Inline_0__")[-1]
        weights[name] = numpy_helper.to_array(node.attribute[0].t)
with open(sys.argv[2], "wb") as out:
    for name in ORDER:
        out.write(np.ascontiguousarray(weights[name], dtype="<f4").tobytes())
