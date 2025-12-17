from extras.scripts import Script, TextVar, FileVar
from dcim.models import Device, Module
from datetime import datetime, date
import csv
import io


class SupportEndFromCSV(Script):
    class Meta:
        name = "Update Support End Date from CSV (Devices & Modules)"
        description = (
            "Paste or upload a CSV export, match objects by the 'SERIAL NUMBER' "
            "column, and update the 'support_end_date' custom field from the "
            "'SERVICE END DATE' column on Devices or Modules."
        )
        commit_default = False  # Only actually save when 'Commit changes' is checked

    csv_data = TextVar(
        description=(
            "Paste CSV content here. The first row must be headers including "
            "'SERIAL NUMBER' and 'SERVICE END DATE'. You can also upload a file "
            "below; both will be concatenated."
        ),
        required=False,
    )

    csv_file = FileVar(
        description=(
            "Optional CSV file upload. The first row must be headers including "
            "'SERIAL NUMBER' and 'SERVICE END DATE'."
        ),
        required=False,
    )

    # Adjust this if your custom field name is different
    CUSTOM_FIELD_NAME = "support_end_date"

    # Header names we care about (matched case-insensitively, ignoring surrounding spaces)
    HEADER_SERIAL = "SERIAL NUMBER"
    HEADER_SERVICE_END = "SERVICE END DATE"

    def _parse_support_date(self, raw: str, lineno: int):
        """
        Parse the support end date string into a date object.

        Accepts a few common formats:
          - YYYY-MM-DD  (recommended)
          - MM/DD/YYYY
          - MM/DD/YY

        Returns:
          - date object if valid
          - None if blank / N/A
        Raises:
          - ValueError if the format is not recognized
        """
        if raw is None:
            return None

        s = raw.strip()
        if not s:
            return None
        if s.upper() in {"N/A", "NA", "NONE"}:
            return None

        # Try some common date formats
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue

        raise ValueError(
            f"Line {lineno}: Unrecognized date format for support_end_date: {s!r}"
        )

    def _find_object_by_serial(self, serial: str, lineno: int):
        """
        Look up either a Device or a Module with this serial.

        Returns:
          (obj, kind, error_message)

          - obj: Device or Module instance, or None
          - kind: 'device' or 'module', or None
          - error_message: None if OK, otherwise a string describing the problem
        """
        dev_qs = Device.objects.filter(serial=serial)
        mod_qs = Module.objects.filter(serial=serial)

        dev_count = dev_qs.count()
        mod_count = mod_qs.count()
        total = dev_count + mod_count

        if total == 0:
            return None, None, (
                f"Line {lineno}: No Device or Module found with serial {serial!r}. Skipping."
            )

        if total > 1:
            details = []
            if dev_count:
                details.append(f"{dev_count} Device(s)")
            if mod_count:
                details.append(f"{mod_count} Module(s)")
            detail_str = ", ".join(details)
            return None, None, (
                f"Line {lineno}: Multiple objects found with serial {serial!r} "
                f"({detail_str}). Skipping."
            )

        if dev_count == 1:
            return dev_qs.first(), "device", None
        else:
            return mod_qs.first(), "module", None

    def _get_combined_csv_text(self, data):
        """
        Return a single CSV text string built from:
          - pasted csv_data (TextVar)
          - uploaded csv_file (FileVar)
        If both are provided, file content is used; pasted data is ignored
        OR you can concatenate – here we choose:
          - If both provided, file + '\n' + text.
        """
        pasted = data.get("csv_data") or ""
        uploaded_text = ""

        csv_file = data.get("csv_file")
        if csv_file:
            # NetBox provides an UploadedFile-like object; read() returns bytes
            uploaded_bytes = csv_file.read()
            try:
                uploaded_text = uploaded_bytes.decode("utf-8")
            except UnicodeDecodeError:
                # Fallback with replacement to avoid crashes
                uploaded_text = uploaded_bytes.decode("utf-8", errors="replace")

        if uploaded_text and pasted:
            # Combine both, file first, then pasted (you can change the order if you prefer)
            return uploaded_text.rstrip("\n") + "\n" + pasted.lstrip("\n")
        elif uploaded_text:
            return uploaded_text
        else:
            return pasted

    def run(self, data, commit):
        combined_csv = self._get_combined_csv_text(data)

        if not combined_csv.strip():
            msg = "No CSV data provided. Paste CSV or upload a CSV file."
            self.log_failure(msg)
            return msg

        f = io.StringIO(combined_csv)

        # DictReader will treat the first row as header
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            msg = "CSV appears to have no header row; cannot continue."
            self.log_failure(msg)
            return msg

        # Build a normalized map from header names to actual fieldnames
        # Normalize by stripping spaces and uppercasing
        normalized_map = {}
        for name in reader.fieldnames:
            norm = (name or "").strip().upper()
            if norm:
                normalized_map[norm] = name

        # Resolve the actual header keys used in this file
        serial_key = normalized_map.get(self.HEADER_SERIAL)
        service_end_key = normalized_map.get(self.HEADER_SERVICE_END)

        if not serial_key:
            msg = (
                f"CSV header row is missing column '{self.HEADER_SERIAL}'. "
                f"Found headers: {reader.fieldnames}"
            )
            self.log_failure(msg)
            return msg

        if not service_end_key:
            msg = (
                f"CSV header row is missing column '{self.HEADER_SERVICE_END}'. "
                f"Found headers: {reader.fieldnames}"
            )
            self.log_failure(msg)
            return msg

        self.log_info(
            f"Using header '{serial_key}' for serial numbers and "
            f"'{service_end_key}' for service end dates."
        )

        total_lines = 0    # data lines (not counting header)
        processed_lines = 0
        updated_devices = 0
        updated_modules = 0
        skipped_no_serial = 0
        skipped_no_object = 0
        skipped_blank_date = 0
        skipped_unchanged_date = 0
        errors = 0

        # DictReader has already consumed the header row; first iteration is line 2
        for lineno, row in enumerate(reader, start=2):
            total_lines += 1

            # Skip completely empty rows
            if not row or all(not (cell or "").strip() for cell in row.values()):
                self.log_info(f"Line {lineno}: Empty/blank line, skipping.")
                continue

            serial_raw = (row.get(serial_key) or "").strip()
            if not serial_raw:
                self.log_warning(
                    f"Line {lineno}: Missing serial in column '{serial_key}'; skipping line."
                )
                skipped_no_serial += 1
                continue

            processed_lines += 1

            # Look up object by serial (Device or Module)
            obj, kind, err = self._find_object_by_serial(serial_raw, lineno)
            if err is not None:
                self.log_failure(err)
                if "No Device or Module found" in err:
                    skipped_no_object += 1
                else:
                    errors += 1
                continue

            support_raw = (row.get(service_end_key) or "").strip()
            try:
                support_date = self._parse_support_date(support_raw, lineno)
            except ValueError as e:
                self.log_failure(str(e))
                errors += 1
                continue

            if support_date is None:
                self.log_info(
                    f"Line {lineno}: No usable service end date for {kind} {obj} "
                    f"(serial {serial_raw!r}); skipping update."
                )
                skipped_blank_date += 1
                continue

            # Ensure custom_field_data exists
            cf_name = self.CUSTOM_FIELD_NAME
            cfd = getattr(obj, "custom_field_data", {}) or {}

            old_value = cfd.get(cf_name)

            # Normalize old_value to ISO date string if possible
            if isinstance(old_value, date):
                old_date_str = old_value.isoformat()
            elif isinstance(old_value, str):
                old_date_str = old_value.strip() or None
            else:
                old_date_str = None

            # Use ISO format date; NetBox will handle this for a Date custom field
            new_value = support_date.isoformat()

            # If the existing date already matches the proposed new date, skip
            if old_date_str == new_value:
                self.log_info(
                    f"Line {lineno}: {kind} '{obj}' (serial {serial_raw!r}) already "
                    f"has support_end_date = {new_value}; skipping."
                )
                skipped_unchanged_date += 1
                continue

            # Actually change the value
            cfd[cf_name] = new_value
            obj.custom_field_data = cfd

            if commit:
                obj.save()

            if kind == "device":
                updated_devices += 1
            elif kind == "module":
                updated_modules += 1

            self.log_success(
                f"Line {lineno}: Updated {kind} '{obj}' (serial {serial_raw!r}) "
                f"{cf_name}: {old_value!r} -> {new_value!r}"
            )

        summary = (
            f"Header row processed. Data lines read: {total_lines}\n"
            f"Lines processed (with serial): {processed_lines}\n"
            f"Devices updated: {updated_devices}\n"
            f"Modules updated: {updated_modules}\n"
            f"Skipped (no serial): {skipped_no_serial}\n"
            f"Skipped (no Device/Module found): {skipped_no_object}\n"
            f"Skipped (blank/N/A date): {skipped_blank_date}\n"
            f"Skipped (unchanged date): {skipped_unchanged_date}\n"
            f"Errors: {errors}"
        )

        self.log_info("=== SUMMARY ===")
        for line in summary.splitlines():
            self.log_info(line)

        return summary
