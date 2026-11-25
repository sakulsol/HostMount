# scene-fakelib

`libScePlayGo.sprx` is drakmor's [pgo_stub](https://github.com/drakmor/pgo_stub) release 0.6 (GPL-3.0),
the same file Kraken Mount bundles (sha256 `f5fdb0ed6cc7413e6dcac79a62e0efd8cfbae04cca0bf932e7a741aed850d213`).
It reports every PlayGo chunk as installed. The starter builder puts it in the fakelib of folder games
whose eboot loads libScePlayGo and that ship none (`BLAZINGMOUNT_SCENE_FAKELIB`, set by HostMount).
