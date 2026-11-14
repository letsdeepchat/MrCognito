# Board recognition (photo → FEN)

The API side is implemented in `app/vision/recognizer.py`:
`OnnxBoardRecognizer` runs an ONNX detector, maps detections through a
homography from the four board corners onto an 8×8 grid, and validates the
resulting FEN with python-chess (missing kings, pawns on back ranks, side not
to move in check are reported back so the UI can ask for manual correction).
`detections_to_fen` is pure and unit-tested.

**No trained weights ship with this repo.** To produce them:

1. Data: label piece boxes (12 classes, order `P N B R Q K p n b r q k`) plus the
   four playing-area corners as keypoints. Mix synthetic renders (randomised
   boards, lighting, camera pose) with a few hundred real phone photos.
2. Train: Ultralytics YOLO detection plus a 4-keypoint pose head for corners,
   e.g. `yolo pose train model=yolo11n-pose.pt data=board.yaml imgsz=640`.
3. Export to ONNX and wrap the graph so it returns
   `out[0] = [N, 6] (cx, cy, w, h, score, cls)` and `out[1] = [4, 2]` corners
   (TL, TR, BR, BL), the contract `OnnxBoardRecognizer` expects.
4. Deploy: `COGNITO_VISION_ONNX_PATH=/models/board.onnx`, install `onnxruntime pillow`.

Client-side inference (the spec's option) is the same model via `onnxruntime-web`;
the server then only validates the FEN through `POST /api/positions`.
