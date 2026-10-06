"""The persona seed the Docker entrypoint uses when ``CC_FINGERPRINT`` is not set.

It used to fall back to one fixed string, ``clearcote-docker``. The seed is the whole identity --
screen, cores, memory, GPU, fonts, canvas noise all derive from it -- so every container started
without ``CC_FINGERPRINT``, for every user of the image, presented the same device, and any two of
them could be linked to each other.

Now each container gets its own random seed, written into the profile directory so the identity
survives ``docker restart`` and, with the profile on a volume, a re-created container too: the
identity lives exactly as long as the cookies and storage it belongs with. A set ``CC_FINGERPRINT``
always wins, including the old ``clearcote-docker`` for anyone who must keep that identity.
"""

import os
import secrets

#: Where the generated seed is kept, inside the browser's profile directory.
SEED_FILE = ".clearcote-seed"
#: The fixed seed every container shared before 0.39.0.
LEGACY_SHARED_SEED = "clearcote-docker"


def container_seed(env_value, profile_dir):
    """``(seed, source)`` for this container.

    ``source`` is ``"env"`` when ``CC_FINGERPRINT`` is set -- including set to an empty value, which
    keeps its old meaning (no seed at all, so no persona) --, ``"saved"`` when a seed generated
    earlier is read back from ``profile_dir``, and ``"new"`` when one is generated now. A new seed is
    saved when the directory is writable; when it is not, the seed still holds for this run, it just
    will not outlive it.
    """
    if env_value is not None:
        return str(env_value), "env"
    path = os.path.join(profile_dir, SEED_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            saved = handle.read().strip()
        if saved:
            return saved, "saved"
    except OSError:
        pass
    seed = "cc-" + secrets.token_hex(8)
    try:
        os.makedirs(profile_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(seed + "\n")
    except OSError:
        pass
    return seed, "new"
