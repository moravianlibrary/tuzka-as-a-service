# shellcheck shell=bash
# taas_admin <args…> — run `taas-admin` inside the api container, wherever the stack is.
#
#   TAAS_ADMIN_MODE=compose (default)  docker compose exec -T api taas-admin …
#   TAAS_ADMIN_MODE=k3d                kubectl exec deploy/<release>-api -- taas-admin …
#                                      (TAAS_NAMESPACE, default: the kube context's;
#                                      TAAS_RELEASE, default taas)
# The mode defaults to TARGET (compose|k3d, as for `make bench`).
#
# Secrets are never passed in argv: name env vars in TAAS_ADMIN_ENV (space-separated) and
# their values are handed to the container's environment, e.g.
#   TAAS_ADMIN_ENV=ENGINE_KEY taas_admin backend upsert --url … --api-key-env ENGINE_KEY
taas_admin() {
  local name env_args=()
  case "${TAAS_ADMIN_MODE:-${TARGET:-compose}}" in
    compose)
      for name in ${TAAS_ADMIN_ENV:-}; do env_args+=(-e "$name=${!name}"); done
      docker compose exec -T "${env_args[@]}" api taas-admin "$@"
      ;;
    k3d)
      for name in ${TAAS_ADMIN_ENV:-}; do env_args+=("$name=${!name}"); done
      kubectl ${TAAS_NAMESPACE:+-n "$TAAS_NAMESPACE"} exec -i "deploy/${TAAS_RELEASE:-taas}-api" -- \
        env "${env_args[@]}" taas-admin "$@"
      ;;
    *)
      echo "taas_admin: TAAS_ADMIN_MODE (or TARGET) must be compose or k3d" >&2
      return 2
      ;;
  esac
}
