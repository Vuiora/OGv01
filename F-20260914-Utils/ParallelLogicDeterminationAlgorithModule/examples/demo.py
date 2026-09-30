"""Run from the module directory: python -m examples.demo."""
from pathlib import Path
from plda import PLDAModule, Tensor, load_program, shard_elementwise
from plda.io import dumps


def main():
    request = load_program(Path(__file__).with_name("request.json"))
    with PLDAModule() as module:
        print(dumps(module.capabilities()))
        plan = module.analyze(request)
        print("TLP levels:", plan.graph.levels)
        print("SIMD:", [(r["id"], r["verdict"]) for r in plan.simd])
        handle = module.submit(request)
        result = handle.result(timeout=60)
        print("Job:", result.state, "output:", result.outputs["result"].values())
        # Explicit partitioning also handles a tail shorter than chunk_size.
        sharded = shard_elementwise("scale", [Tensor.from_values(range(10))], 4, params={"factor": 2})
        print("Shards:", module.run(sharded).outputs["result"].values())


if __name__ == "__main__":
    main()
