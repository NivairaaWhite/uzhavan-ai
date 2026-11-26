import 'dart:io';
import 'package:flutter/services.dart' show rootBundle;
import 'package:tflite_flutter/tflite_flutter.dart';
import 'package:image/image.dart' as img;
import '../models/crop_listing.dart';

/// On-device crop grading. Loads the quantized TFLite model shipped in
/// assets/models/crop_grader.tflite (see ai_model/ for the training +
/// export pipeline that produces this file). Runs entirely offline —
/// this is the "works with zero network" half of the app.
///
/// The output layout is read from the model itself instead of being assumed:
///  * `[1, numClasses]`            – a classification head (YOLOv8-cls etc.)
///  * `[1, 4 + numClasses, N]` or
///    `[1, N, 4 + numClasses]`     – the YOLOv8 *detection* head that
///                                   ai_model/train_yolo.py produces by
///                                   default. The grade is the class with the
///                                   highest peak score over all N anchors.
/// The original code only handled the first layout, so a model exported by
/// the repo's own pipeline failed at the first scan with a shape error.
///
/// NOTE: crop_grader.tflite is NOT included in this repo — a model file
/// only becomes meaningful once trained on real, labeled crop-quality
/// images (see ai_model/README.md).
class TfliteService {
  static const String modelPath = 'assets/models/crop_grader.tflite';
  static const String labelsPath = 'assets/models/labels.txt';

  Interpreter? _interpreter;
  List<String> _labels = [];
  int _inputHeight = 224;
  int _inputWidth = 224;

  /// Why the model could not be loaded (null when it loaded fine). Shown in
  /// the UI so "model not loaded" is diagnosable instead of a dead end.
  String? loadError;

  bool get isLoaded => _interpreter != null;

  Future<void> loadModel() async {
    Interpreter? interpreter;
    try {
      interpreter = await Interpreter.fromAsset(modelPath);

      // Bundled assets live in the app bundle, not the filesystem — must
      // go through rootBundle, not dart:io File. trim() matters: a labels
      // file saved with Windows (CRLF) endings would otherwise leave a
      // trailing '\r' on every label and no grade would ever match.
      final raw = await rootBundle.loadString(labelsPath);
      final labels = raw
          .split('\n')
          .map((l) => l.trim())
          .where((l) => l.isNotEmpty)
          .toList();
      if (labels.isEmpty) {
        throw StateError('labels.txt is empty');
      }

      final inShape = interpreter.getInputTensor(0).shape; // expected [1, H, W, 3]
      if (inShape.length != 4 || inShape[3] != 3) {
        throw StateError('Unsupported model input shape $inShape (expected [1, H, W, 3])');
      }
      final inType = interpreter.getInputTensor(0).type.toString().toLowerCase();
      if (!inType.contains('float32')) {
        throw StateError(
            'Model input type is $inType but this app feeds float32. Re-export with '
            'float32 input/output (INT8 weights are fine).');
      }

      _inputHeight = inShape[1];
      _inputWidth = inShape[2];
      _labels = labels;
      _interpreter = interpreter;
      loadError = null;
    } catch (e) {
      // Model not present yet (or unusable) — app still runs, scanning is
      // just disabled until ai_model/export_tflite.py output is added to
      // assets/.
      interpreter?.close();
      _interpreter = null;
      loadError = e.toString();
    }
  }

  /// Returns (grade, confidence). The grade is always one the backend
  /// accepts (grade_a | grade_b | grade_c | rejected) — defect classes are
  /// folded into `rejected`. Throws if the model isn't loaded — the UI
  /// should check [isLoaded] first and show a clear "model not ready"
  /// state instead of calling this blindly.
  Future<(String, double)> classify(File imageFile) async {
    final interpreter = _interpreter;
    if (interpreter == null) {
      throw StateError('TFLite model not loaded — see ai_model/README.md');
    }
    final bytes = await imageFile.readAsBytes();
    var decoded = img.decodeImage(bytes);
    if (decoded == null) throw Exception('Could not decode image');
    // Phone cameras store rotation in EXIF rather than rotating pixels; without
    // this a portrait photo reaches the model sideways.
    decoded = img.bakeOrientation(decoded);

    final resized = img.copyResize(decoded, width: _inputWidth, height: _inputHeight);
    final input = _imageToTensor(resized);

    final outShape = interpreter.getOutputTensor(0).shape;
    final outCount = outShape.fold<int>(1, (a, b) => a * b);
    final output = List.filled(outCount, 0.0).reshape(outShape);

    interpreter.run(input, output);

    final scores = _classScores(_flatten(output), outShape);
    var bestIdx = 0;
    for (var i = 1; i < scores.length; i++) {
      if (scores[i] > scores[bestIdx]) bestIdx = i;
    }
    return (CropListing.normaliseGrade(_labels[bestIdx]), scores[bestIdx]);
  }

  /// Per-class confidence from either supported output layout.
  List<double> _classScores(List<double> flat, List<int> shape) {
    final n = _labels.length;

    // Classification head: [1, numClasses]
    if (shape.length == 2) {
      if (shape[1] != n) {
        throw StateError('Model outputs ${shape[1]} classes but labels.txt lists $n.');
      }
      return flat;
    }

    // YOLOv8 detection head: rows 0-3 are the box, the rest are class scores
    // (already sigmoid-ed by the Ultralytics export).
    if (shape.length == 3) {
      final channels = 4 + n;
      final bool channelsFirst;
      final int anchors;
      if (shape[1] == channels) {
        channelsFirst = true;
        anchors = shape[2];
      } else if (shape[2] == channels) {
        channelsFirst = false;
        anchors = shape[1];
      } else {
        throw StateError(
            'Output shape $shape does not match ${4 + n} channels (4 box + $n classes from labels.txt). '
            'Does labels.txt match the classes the model was trained on?');
      }
      final best = List<double>.filled(n, 0.0);
      for (var a = 0; a < anchors; a++) {
        for (var c = 0; c < n; c++) {
          final v = channelsFirst ? flat[(4 + c) * anchors + a] : flat[a * channels + 4 + c];
          if (v > best[c]) best[c] = v;
        }
      }
      return best;
    }

    throw StateError('Unsupported model output shape $shape');
  }

  /// Flattens whatever nested List structure the interpreter filled in
  /// (typed or dynamic) into row-major doubles, avoiding fragile casts such
  /// as `as List<double>` on a reshaped buffer.
  List<double> _flatten(dynamic value) {
    final out = <double>[];
    void walk(dynamic x) {
      if (x is List) {
        for (final e in x) {
          walk(e);
        }
      } else if (x is num) {
        out.add(x.toDouble());
      }
    }

    walk(value);
    return out;
  }

  List<List<List<List<double>>>> _imageToTensor(img.Image image) {
    return [
      List.generate(
        _inputHeight,
        (y) => List.generate(_inputWidth, (x) {
          final pixel = image.getPixel(x, y);
          return [pixel.r / 255.0, pixel.g / 255.0, pixel.b / 255.0];
        }),
      ),
    ];
  }

  void dispose() => _interpreter?.close();
}
