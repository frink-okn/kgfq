import httpx


def test_two_servers_serve_two_graphs(chem, genes):
    assert chem.origin != genes.origin
    a = httpx.get(chem.origin + "/", headers={"Accept": "application/json"}).json()
    b = httpx.get(genes.origin + "/", headers={"Accept": "application/json"}).json()
    assert [d["id"] for d in a["datasets"]] == ["chemkg"]
    assert [d["id"] for d in b["datasets"]] == ["genekg"]
    assert a["caps"]["max_bindings"] >= 1
