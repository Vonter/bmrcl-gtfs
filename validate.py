import subprocess

# gtfs-validator
subprocess.run(["java", "-jar", "tools/gtfs-validator/gtfs-validator-7.0.0-cli.jar", "-i", "gtfs/bmrcl.zip", "-o", "validation/gtfs-validator"])
