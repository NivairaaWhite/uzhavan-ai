/// Mirrors the backend's CropListingCreate/Out schema (see
/// backend/app/schemas.py) plus a few fields that only make sense on the
/// device (sync state, local offline uuid).
class CropListing {
  final String? id; // null until the server assigns one
  final String offlineScanUuid; // generated on-device, used to dedupe on sync
  final String cropType;
  final String grade; // grade_a | grade_b | grade_c | rejected
  final double? gradeConfidence;
  final double quantityKg;
  final double? targetPricePerKg;
  final double? fairPricePerKg;
  final double latitude;
  final double longitude;
  final String? scanImageRef;
  final DateTime createdAt;
  bool synced;

  /// Grades the backend's CropGrade enum accepts. The on-device model can
  /// also emit labels such as `defect_rotten`, `defect_pest_damage` and
  /// `foreign_matter` (see ai_model/data.yaml); sending those verbatim makes
  /// the server answer 422 for the WHOLE sync batch, so everything that is
  /// not a saleable grade is reported as `rejected`.
  static const Set<String> backendGrades = {'grade_a', 'grade_b', 'grade_c', 'rejected'};

  static String normaliseGrade(String label) {
    final g = label.trim();
    return backendGrades.contains(g) ? g : 'rejected';
  }

  /// False for scans the server is certain to refuse (it validates
  /// quantity > 0 and real coordinates). One such row must not be allowed to
  /// block the rest of the queue.
  bool get isUploadable =>
      quantityKg > 0 &&
      latitude >= -90 &&
      latitude <= 90 &&
      longitude >= -180 &&
      longitude <= 180 &&
      !(latitude == 0 && longitude == 0); // 0,0 = "no GPS fix" placeholder from older builds

  CropListing({
    this.id,
    required this.offlineScanUuid,
    required this.cropType,
    required this.grade,
    this.gradeConfidence,
    required this.quantityKg,
    this.targetPricePerKg,
    this.fairPricePerKg,
    required this.latitude,
    required this.longitude,
    this.scanImageRef,
    required this.createdAt,
    this.synced = false,
  });

  Map<String, dynamic> toJson() => {
        'crop_type': cropType,
        'grade': normaliseGrade(grade),
        'grade_confidence': gradeConfidence,
        'quantity_kg': quantityKg,
        'target_price_per_kg': targetPricePerKg,
        'latitude': latitude,
        'longitude': longitude,
        'scan_image_ref': scanImageRef,
        'offline_scan_uuid': offlineScanUuid,
      };

  factory CropListing.fromJson(Map<String, dynamic> json) => CropListing(
        id: json['id'] as String?,
        offlineScanUuid: json['offline_scan_uuid'] as String? ?? '',
        cropType: json['crop_type'] as String,
        grade: json['grade'] as String,
        gradeConfidence: (json['grade_confidence'] as num?)?.toDouble(),
        quantityKg: (json['quantity_kg'] as num).toDouble(),
        targetPricePerKg: (json['target_price_per_kg'] as num?)?.toDouble(),
        fairPricePerKg: (json['fair_price_per_kg'] as num?)?.toDouble(),
        latitude: (json['latitude'] as num).toDouble(),
        longitude: (json['longitude'] as num).toDouble(),
        scanImageRef: json['scan_image_ref'] as String?,
        createdAt: DateTime.tryParse(json['created_at'] as String? ?? '') ?? DateTime.now(),
        synced: json['id'] != null,
      );

  Map<String, dynamic> toHiveMap() => {
        'id': id,
        'offlineScanUuid': offlineScanUuid,
        'cropType': cropType,
        'grade': grade,
        'gradeConfidence': gradeConfidence,
        'quantityKg': quantityKg,
        'targetPricePerKg': targetPricePerKg,
        'fairPricePerKg': fairPricePerKg,
        'latitude': latitude,
        'longitude': longitude,
        'scanImageRef': scanImageRef,
        'createdAt': createdAt.toIso8601String(),
        'synced': synced,
      };

  factory CropListing.fromHiveMap(Map<dynamic, dynamic> map) => CropListing(
        id: map['id'] as String?,
        offlineScanUuid: map['offlineScanUuid'] as String,
        cropType: map['cropType'] as String,
        grade: map['grade'] as String,
        gradeConfidence: map['gradeConfidence'] as double?,
        quantityKg: map['quantityKg'] as double,
        targetPricePerKg: map['targetPricePerKg'] as double?,
        fairPricePerKg: map['fairPricePerKg'] as double?,
        latitude: map['latitude'] as double,
        longitude: map['longitude'] as double,
        scanImageRef: map['scanImageRef'] as String?,
        createdAt: DateTime.parse(map['createdAt'] as String),
        synced: map['synced'] as bool? ?? false,
      );
}
