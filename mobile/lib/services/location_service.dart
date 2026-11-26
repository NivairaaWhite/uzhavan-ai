import 'package:geolocator/geolocator.dart';

/// One place that handles the location dance every screen needs: is GPS on,
/// has the user granted permission (asking if not), and a graceful fallback
/// to the last known fix. Never throws.
///
/// The previous code called Geolocator directly and never requested
/// permission, so on a fresh install every lookup failed and scans were
/// silently saved at latitude 0 / longitude 0.
class LocationService {
  static Future<Position?> currentPosition() async {
    try {
      if (!await Geolocator.isLocationServiceEnabled()) {
        return await Geolocator.getLastKnownPosition();
      }

      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }
      if (permission == LocationPermission.denied ||
          permission == LocationPermission.deniedForever) {
        return null;
      }

      try {
        // GPS works without any network, so this is fine in the field.
        return await Geolocator.getCurrentPosition(
          locationSettings: const LocationSettings(
            accuracy: LocationAccuracy.high,
            timeLimit: Duration(seconds: 15),
          ),
        );
      } catch (_) {
        return await Geolocator.getLastKnownPosition();
      }
    } catch (_) {
      return null;
    }
  }
}
