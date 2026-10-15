"""Platform driver base class.

A driver owns the personality bits that vary between network operating
systems, so the shared machinery (Console, orchestrator, views) never
hardcodes one NOS:

  - prompt regexes (Cisco: ``>`` user exec / ``#`` priv exec; PAN-OS inverts
    the convention: ``>`` is operational mode, ``#`` is configure mode)
  - the netmiko device_type the console session is promoted to
  - persist semantics (``write memory`` vs ``commit`` + job poll)
  - device classification → which configured pipeline runs

What does NOT belong here: step logic. Each NOS family gets its own step
modules in jobs/steps/ (the existing steps are the Cisco IOS family), and its
pipelines reference only that family's steps — a driver never changes another
family's steps.

Adding a NOS (e.g. PAN-OS firewalls):
  1. subclass PlatformDriver in this package,
  2. register it in DRIVER_REGISTRY (drivers/__init__.py) under the
     Platform.network_driver values it serves,
  3. add its pipeline setting (e.g. ``pipeline_firewall``) + device class,
  4. add its step family to jobs/steps/ and reference it from that pipeline.
"""


class PlatformDriver:
    """One driver per NOS family, resolved from Platform.network_driver."""

    #: Platform.network_driver values this driver serves.
    network_drivers = ()

    #: netmiko device_type the raw console session is redispatched to once a
    #: device prompt is reachable (Console.promote()).
    netmiko_device_type = None

    #: Prompt regexes for raw expect()-driven interaction.
    exec_prompt = None
    cfg_prompt = None
    any_prompt = None

    #: device_class → settings key holding that class's pipeline.
    pipeline_keys = {}

    def classify(self, device):
        """Return the device_class string for this device, or None if the
        driver can't place it (the orchestrator skips the device)."""
        raise NotImplementedError

    def pipeline_key(self, device_class):
        """Settings key of the pipeline that runs for a device class."""
        return self.pipeline_keys[device_class]

    def save_config(self, console, verify_contains=None):
        """Persist the running config (NOS-specific: write memory vs commit).

        Receives the Console so drivers can drive the session and self-heal
        platform quirks. Must raise on a save that did not persist.
        `verify_contains`, when a caller passes it, is text that must appear
        in the persisted config afterwards — positive proof the save landed,
        for drivers that can read the saved config back.
        """
        raise NotImplementedError
