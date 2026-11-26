import 'dart:io';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:camera/camera.dart';
import 'package:uuid/uuid.dart';
import '../services/tflite_service.dart';
import '../services/local_db_service.dart';
import '../services/location_service.dart';
import '../models/crop_listing.dart';
import 'results_screen.dart';

/// The core offline flow: point camera at produce -> on-device TFLite
/// grades it -> result is written to Hive immediately, before any network
/// call. This works with the phone in airplane mode.
class ScanScreen extends StatefulWidget {
  const ScanScreen({super.key});
  @override
  State<ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends State<ScanScreen> {
  CameraController? _cameraController;
  String? _cameraError;
  bool _busy = false;
  String? _cropType = 'tomato';
  final _quantityCtrl = TextEditingController(text: '10');

  @override
  void initState() {
    super.initState();
    _initCamera();
  }

  Future<void> _initCamera() async {
    try {
      final cameras = await availableCameras();
      if (cameras.isEmpty) {
        if (mounted) setState(() => _cameraError = 'No camera found on this device.');
        return;
      }
      _cameraController = CameraController(cameras.first, ResolutionPreset.medium);
      await _cameraController!.initialize();
      if (mounted) setState(() {});
    } catch (e) {
      // Typically: camera permission denied. Without this the screen sat on
      // a spinner forever.
      if (mounted) setState(() => _cameraError = 'Camera unavailable: $e');
    }
  }

  Future<void> _captureAndGrade() async {
    final tflite = context.read<TfliteService>();
    final localDb = context.read<LocalDbService>();
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);

    if (!tflite.isLoaded) {
      final why = tflite.loadError == null ? '' : '\n${tflite.loadError}';
      messenger.showSnackBar(
        SnackBar(content: Text('Grading model not loaded yet — see ai_model/README.md$why')),
      );
      return;
    }
    if (_cameraController == null || !_cameraController!.value.isInitialized) return;

    // The backend rejects non-positive quantities for the WHOLE sync batch,
    // so never queue one.
    final quantity = double.tryParse(_quantityCtrl.text.trim());
    if (quantity == null || quantity <= 0) {
      messenger.showSnackBar(const SnackBar(content: Text('Enter a quantity above 0 kg first.')));
      return;
    }

    setState(() => _busy = true);
    try {
      final picture = await _cameraController!.takePicture();
      final (grade, confidence) = await tflite.classify(File(picture.path));

      // Asks for permission if needed. A scan without a real location can't
      // be pooled for logistics, and the old fallback of 0,0 put it in the
      // Atlantic — so ask the farmer to fix it rather than save garbage.
      final position = await LocationService.currentPosition();
      if (position == null) {
        messenger.showSnackBar(const SnackBar(
          content: Text('Location is needed to save a scan. Turn on GPS and allow location access, then try again.'),
        ));
        return;
      }

      final listing = CropListing(
        offlineScanUuid: const Uuid().v4(),
        cropType: _cropType ?? 'unknown',
        grade: grade,
        gradeConfidence: confidence,
        quantityKg: quantity,
        latitude: position.latitude,
        longitude: position.longitude,
        scanImageRef: picture.path,
        createdAt: DateTime.now(),
      );

      await localDb.saveListing(listing);

      if (!mounted) return;
      navigator.pushReplacement(
        MaterialPageRoute(builder: (_) => ResultsScreen(listing: listing)),
      );
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('Could not grade this scan: $e')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _cameraController?.dispose();
    _quantityCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Scan Crop')),
      body: Column(
        children: [
          Expanded(
            child: _cameraController?.value.isInitialized == true
                ? CameraPreview(_cameraController!)
                : Center(
                    child: _cameraError != null
                        ? Padding(
                            padding: const EdgeInsets.all(24),
                            child: Text(_cameraError!, textAlign: TextAlign.center),
                          )
                        : const CircularProgressIndicator(),
                  ),
          ),
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              children: [
                DropdownButtonFormField<String>(
                  initialValue: _cropType,
                  decoration: const InputDecoration(labelText: 'Crop type'),
                  items: const [
                    DropdownMenuItem(value: 'tomato', child: Text('Tomato')),
                    DropdownMenuItem(value: 'onion', child: Text('Onion')),
                    DropdownMenuItem(value: 'paddy', child: Text('Paddy')),
                    DropdownMenuItem(value: 'brinjal', child: Text('Brinjal')),
                    DropdownMenuItem(value: 'banana', child: Text('Banana')),
                    DropdownMenuItem(value: 'chilli', child: Text('Chilli')),
                  ],
                  onChanged: (v) => setState(() => _cropType = v),
                ),
                TextField(
                  controller: _quantityCtrl,
                  keyboardType: TextInputType.number,
                  decoration: const InputDecoration(labelText: 'Quantity (kg)'),
                ),
                const SizedBox(height: 12),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton.icon(
                    onPressed: _busy ? null : _captureAndGrade,
                    icon: const Icon(Icons.camera),
                    label: Text(_busy ? 'Grading...' : 'Capture & Grade'),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
