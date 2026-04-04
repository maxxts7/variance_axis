"""Quick profiling script to diagnose slow generation on H100."""

import time
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = "Qwen/Qwen3-32B"
PROMPT = "What is the capital of France?"
MAX_NEW = 32

print(f"Loading model: {MODEL}")
t0 = time.time()
model = AutoModelForCausalLM.from_pretrained(
    MODEL, torch_dtype=torch.bfloat16, device_map="auto", attn_implementation="flash_attention_2"
)
model.eval()
tokenizer = AutoTokenizer.from_pretrained(MODEL)
print(f"Model loaded in {time.time()-t0:.1f}s")

# Check device map
if hasattr(model, "hf_device_map"):
    devices = set(model.hf_device_map.values())
    print(f"Device map uses: {devices}")
else:
    print(f"Model device: {next(model.parameters()).device}")

# Check if flash attention is enabled
print(f"Model class: {type(model).__name__}")
try:
    attn_impl = model.config._attn_implementation
    print(f"Attention implementation: {attn_impl}")
except:
    print("Could not determine attention implementation")

# Tokenize
conversation = [{"role": "user", "content": PROMPT}]
text = tokenizer.apply_chat_template(conversation, tokenize=False, add_generation_prompt=True, enable_thinking=False)
input_ids = tokenizer(text, return_tensors="pt")["input_ids"].to(model.device if not hasattr(model, 'hf_device_map') else "cuda:0")
print(f"Input tokens: {input_ids.shape[1]}")

# --- Test 1: Plain generate (no hooks, no scores) ---
print("\n=== Test 1: Plain generate (no hooks, no output_scores) ===")
torch.cuda.synchronize()
t0 = time.time()
with torch.inference_mode():
    out = model.generate(input_ids, max_new_tokens=MAX_NEW, do_sample=False)
torch.cuda.synchronize()
dt = time.time() - t0
n_tok = out.shape[1] - input_ids.shape[1]
print(f"  {n_tok} tokens in {dt:.2f}s ({n_tok/dt:.1f} tok/s)")
del out; torch.cuda.empty_cache()

# --- Test 2: With output_scores=True ---
print("\n=== Test 2: With output_scores=True ===")
torch.cuda.synchronize()
t0 = time.time()
with torch.inference_mode():
    out = model.generate(input_ids, max_new_tokens=MAX_NEW, do_sample=False,
                         output_scores=True, return_dict_in_generate=True)
torch.cuda.synchronize()
dt = time.time() - t0
n_tok = out.sequences.shape[1] - input_ids.shape[1]
print(f"  {n_tok} tokens in {dt:.2f}s ({n_tok/dt:.1f} tok/s)")
print(f"  Scores shape per step: {out.scores[0].shape}")
del out; torch.cuda.empty_cache()

# --- Test 3: With a simple forward hook (like the perturbation hook) ---
print("\n=== Test 3: With perturbation-style hook on layer 32 ===")
layers = model.model.layers
delta = torch.randn(model.config.hidden_size, dtype=torch.bfloat16, device="cuda:0") * 0.01

def hook_fn(module, input, output):
    hidden = output[0] if isinstance(output, tuple) else output
    hidden[:, -1, :] += delta
    return (hidden, *output[1:]) if isinstance(output, tuple) else hidden

handle = layers[32].register_forward_hook(hook_fn)
torch.cuda.synchronize()
t0 = time.time()
with torch.inference_mode():
    out = model.generate(input_ids, max_new_tokens=MAX_NEW, do_sample=False,
                         output_scores=True, return_dict_in_generate=True)
torch.cuda.synchronize()
dt = time.time() - t0
handle.remove()
n_tok = out.sequences.shape[1] - input_ids.shape[1]
print(f"  {n_tok} tokens in {dt:.2f}s ({n_tok/dt:.1f} tok/s)")
del out; torch.cuda.empty_cache()

# --- Test 4: With 3 hooks (perturbation + 2 trackers, like the real experiment) ---
print("\n=== Test 4: With 3 hooks (perturbation + 2 projection trackers) ===")
axis32 = torch.randn(model.config.hidden_size, dtype=torch.float32, device="cuda:0")
axis63 = torch.randn(model.config.hidden_size, dtype=torch.float32, device="cuda:0")
projs = []

def perturb_hook(module, input, output):
    hidden = output[0] if isinstance(output, tuple) else output
    hidden[:, -1, :] += delta
    return (hidden, *output[1:]) if isinstance(output, tuple) else hidden

def tracker_hook_factory(axis_vec):
    local_projs = []
    projs.append(local_projs)
    def hook_fn(module, input, output):
        act = output[0] if isinstance(output, tuple) else output
        h = act[0, -1, :].detach().float()
        local_projs.append((h @ axis_vec).item())
    return hook_fn

h1 = layers[32].register_forward_hook(perturb_hook)
h2 = layers[32].register_forward_hook(tracker_hook_factory(axis32))
h3 = layers[63].register_forward_hook(tracker_hook_factory(axis63))

torch.cuda.synchronize()
t0 = time.time()
with torch.inference_mode():
    out = model.generate(input_ids, max_new_tokens=MAX_NEW, do_sample=False,
                         output_scores=True, return_dict_in_generate=True)
torch.cuda.synchronize()
dt = time.time() - t0
h1.remove(); h2.remove(); h3.remove()
n_tok = out.sequences.shape[1] - input_ids.shape[1]
print(f"  {n_tok} tokens in {dt:.2f}s ({n_tok/dt:.1f} tok/s)")
del out; torch.cuda.empty_cache()

# --- Test 5: Check if thinking mode is accidentally on ---
print("\n=== Test 5: Check generation output for thinking tokens ===")
with torch.inference_mode():
    out = model.generate(input_ids, max_new_tokens=MAX_NEW, do_sample=False)
full_text = tokenizer.decode(out[0], skip_special_tokens=False)
print(f"  Full output (with special tokens): {full_text[:500]}")
if "<think>" in full_text:
    print("  WARNING: Thinking mode is ON — model generates hidden thinking tokens before answering!")
    print("  This could massively inflate generation time.")
else:
    print("  No thinking tokens detected.")

# --- Test 6: With thinking suppressed ---
print("\n=== Test 6: Force-close thinking block ===")
text_no_think = text + "<think>\n</think>\n\n"
input_ids_no_think = tokenizer(text_no_think, return_tensors="pt")["input_ids"].to("cuda:0")
print(f"  Input tokens (with think close): {input_ids_no_think.shape[1]}")
torch.cuda.synchronize()
t0 = time.time()
with torch.inference_mode():
    out = model.generate(input_ids_no_think, max_new_tokens=MAX_NEW, do_sample=False)
torch.cuda.synchronize()
dt = time.time() - t0
n_tok = out.shape[1] - input_ids_no_think.shape[1]
full = tokenizer.decode(out[0], skip_special_tokens=False)
print(f"  {n_tok} tokens in {dt:.2f}s ({n_tok/dt:.1f} tok/s)")
print(f"  Output: {full[-200:]}")
if "<think>" not in full[len(text_no_think):]:
    print("  SUCCESS: No thinking tokens in generation.")
else:
    print("  Still generating thinking tokens.")

# --- Test 7: Longer generation to get steady-state throughput ---
print("\n=== Test 7: 128 tokens, no hooks, with think suppression ===")
torch.cuda.synchronize()
t0 = time.time()
with torch.inference_mode():
    out = model.generate(input_ids_no_think, max_new_tokens=128, do_sample=False)
torch.cuda.synchronize()
dt = time.time() - t0
n_tok = out.shape[1] - input_ids_no_think.shape[1]
print(f"  {n_tok} tokens in {dt:.2f}s ({n_tok/dt:.1f} tok/s)")
del out; torch.cuda.empty_cache()

# --- Test 8: Check actual attention implementation ---
print("\n=== Test 8: Attention implementation check ===")
print(f"  config._attn_implementation: {getattr(model.config, '_attn_implementation', 'N/A')}")
layer0 = model.model.layers[0]
attn = layer0.self_attn
print(f"  Attention class: {type(attn).__name__}")
print(f"  Attention module: {type(attn).__module__}")

# --- Test 9: Check CUDA and torch compile status ---
print("\n=== Test 9: System info ===")
print(f"  PyTorch: {torch.__version__}")
print(f"  CUDA: {torch.version.cuda}")
print(f"  GPU: {torch.cuda.get_device_name(0)}")
print(f"  GPU memory total: {torch.cuda.get_device_properties(0).total_mem / 1e9:.1f} GB")
print(f"  GPU memory used: {torch.cuda.memory_allocated(0) / 1e9:.1f} GB")
print(f"  GPU memory reserved: {torch.cuda.memory_reserved(0) / 1e9:.1f} GB")
try:
    import flash_attn
    print(f"  flash_attn version: {flash_attn.__version__}")
except ImportError:
    print(f"  flash_attn: NOT INSTALLED")

# --- Test 10: Raw forward pass speed (no generation overhead) ---
print("\n=== Test 10: Raw forward pass latency (single token decode) ===")
# Simulate a single decode step
dummy_input = torch.randint(0, 1000, (1, 1), device="cuda:0")
# Warmup
with torch.inference_mode():
    _ = model(dummy_input, use_cache=False)
torch.cuda.synchronize()
times = []
for _ in range(10):
    torch.cuda.synchronize()
    t0 = time.time()
    with torch.inference_mode():
        _ = model(dummy_input, use_cache=False)
    torch.cuda.synchronize()
    times.append(time.time() - t0)
avg = sum(times) / len(times)
print(f"  Avg forward pass (1 token, no KV cache): {avg*1000:.1f}ms")
print(f"  Theoretical max tok/s: {1/avg:.1f}")

print("\nDone.")
