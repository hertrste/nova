from oslo_config import cfg


chv_opt_group = cfg.OptGroup('chv',
                             title='CHV Options',
                             help="""
Allows to configure Cloud Hypervisor.

These options are used when the compute_driver is set to use
CHV (compute_driver=chv.CHVDriver)
""")


chv_opts = []


def register_opts(conf):
    conf.register_group(chv_opt_group)
    conf.register_opts(chv_opts, group=chv_opt_group)


def list_opts():
    return {chv_opt_group: chv_opts}
