import logging
import os
import time
import math
from util.DockerUtil import getContainerStat, createContainers, get_host_config, \
    get_network_config, remove_container, restart_container
from util.DBUtil import selectByKey, bulk_insert, insert, selectAll, bulk_delete
from util.NginxUtil import get_nginx_config, nginx_reload
from util.DockerUtil import Container_status, getContainersNameFromDB
from util.ConfigUtil import Properties

LOG_FILE_PATH = '/data0/log/DCP.log'

os.system("mkdir -p /data0/log")

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s',
                    datefmt='%a, %d %b %Y %H:%M:%S',
                    filename=LOG_FILE_PATH,
                    filemode='a')

DCP_DB_PATH = "dcp_container"
DCP_CONF_PATH = "dcp_conf"

DCP_DEPLOY_CONF = Properties("../conf/dcp_deploy.conf").getProperties()
DCP_MAX_OVERLOAD = DCP_DEPLOY_CONF["dcp.max.overload"]
DCP_MIN_OVERLOAD = DCP_DEPLOY_CONF["dcp.min.overload"]


def getAllContainersPercent():
    container_list = getContainersNameFromDB(DCP_DB_PATH)
    logging.info("current containers in falcon " + str(container_list))
    container_stats_list = {}

    for container_name in container_list:
        container_stats = getContainerStat(container_name)
        # CPU
        cpu_total_usage = container_stats['cpu_stats']['cpu_usage']['total_usage']
        cpu_system_uasge = container_stats['cpu_stats']['system_cpu_usage']
        cpu_percent = round((float(cpu_total_usage) / float(cpu_system_uasge)), 10)
        # Memory
        mem_usage = container_stats['memory_stats']['usage']
        mem_limit = container_stats['memory_stats']['limit']
        mem_percent = round(float(mem_usage) / float(mem_limit), 10)
        # IP
        container_ip = os.popen("docker inspect --format '{{ .NetworkSettings.IPAddress }}' " + container_name).read()

        container = Container_status(container_name, mem_percent, cpu_percent, container_ip)
        container_stats_list[container_name] = container

    return container_stats_list


def current_platform_stats():
    overload_containers = []
    container_stats_list = getAllContainersPercent()

    container_platform_mem_stats = float(0)
    container_platform_cpu_stats = float(0)

    for container in container_stats_list.values():
        container_platform_mem_stats += float(container.mem_usage_percent)
        container_platform_cpu_stats += float(container.cpu_usage_percent)
        if container.mem_usage_percent > float(DCP_MAX_OVERLOAD):
            overload_containers.append(container)
        elif container.cpu_usage_percent > float(DCP_MIN_OVERLOAD):
            overload_containers.append(container)
        else:
            continue

    container_platform_cpu_stats_percent = container_platform_cpu_stats / float(len(container_stats_list))
    container_platform_mem_stats_percent = container_platform_mem_stats / float(len(container_stats_list))

    print "current cpu:" + str(container_platform_cpu_stats_percent)
    print "current mem:" + str(container_platform_mem_stats_percent)

    if container_platform_mem_stats_percent > float(DCP_MAX_OVERLOAD):
        logging.warn("The mem of platform is over " + DCP_MAX_OVERLOAD + ", current is " + str(
            container_platform_mem_stats / float(
                len(container_stats_list))))
        return "mem_warning"

    if container_platform_cpu_stats_percent > float(DCP_MAX_OVERLOAD):
        logging.warn("The cpu of platform is over " + DCP_MAX_OVERLOAD + ", current is " + str(
            container_platform_cpu_stats / float(
                len(container_stats_list))))
        return "cpu_warning"

    if container_platform_mem_stats_percent < float(DCP_MIN_OVERLOAD):
        logging.warn("The mem of platform is less than " + DCP_MIN_OVERLOAD + ", current is " + str(
            container_platform_mem_stats / float(
                len(container_stats_list))))
        return "mem_low_warning"

    return overload_containers


def falcon():
    status = current_platform_stats()

    if len(status) == 0:
        logging.info("healthy!")
        return
    elif status == "mem_warning":
        logging.warn("mem usage over " + DCP_MAX_OVERLOAD + "!")
        takeAddStrategy()
    elif status == "cpu_warning":
        logging.warn("cpu_usage over " + DCP_MAX_OVERLOAD + "!")
        takeAddStrategy()
    elif status == "mem_low_warning":
        logging.warn("mem usage under " + DCP_MIN_OVERLOAD + "!")
        takeReduceStrategy()
    else:
        logging.warn(str(status) + " need to adjust!")
        # balance(status)


def balance(overload_containers):
    for container in overload_containers:
        logging.info("restart " + container.name)
        restart_container(container.name)


def takeAddStrategy():
    logging.info("Add containers to solve...")
    count = 1
    flag = True
    # get app_start_work
    app_start_network = selectByKey(DCP_CONF_PATH, "app_start_network")
    app_start_host = int(selectByKey(DCP_CONF_PATH, "app_start_host"))
    # get app info
    app_container_image = selectByKey(DCP_CONF_PATH, "app_container_image")
    app_container_mem = selectByKey(DCP_CONF_PATH, "app_container_mem")
    app_network_name = selectByKey(DCP_CONF_PATH, "app_network_name")

    while flag:
        container_dict = {}

        app_container_num = int(math.pow(2, count))
        logging.info("Add " + str(app_container_num) + " containers")

        pre_app_container_num = int(selectByKey(DCP_CONF_PATH, "app_container_num"))

        cur_app_container_num = pre_app_container_num + app_container_num
        # create container
        for i in range(pre_app_container_num, cur_app_container_num):
            app_container_name = "APP-" + str(i)
            # publish port = 80
            app_host_config = get_host_config(mem_limit=app_container_mem, ports={80: None})
            # ! IPv4 address
            host_num = int(app_start_host) + i
            if host_num > 255:
                logging.warn("Add container failed,this subnet is full")
                return
            app_ip_address = app_start_network + "." + str(host_num)
            app_network_config = get_network_config(app_network_name, app_ip_address)

            createContainers(image=app_container_image, name=app_container_name,
                             host_config=app_host_config, networking_config=app_network_config)

            container_dict[app_container_name] = app_ip_address

        insert(DCP_CONF_PATH, "app_container_num", str(cur_app_container_num))
        bulk_insert(DCP_DB_PATH, container_dict)

        container_dict = selectAll(DCP_DB_PATH)

        config = get_nginx_config(container_dict)
        nginx_reload(config)
        os.system("/usr/local/nginx/sbin/nginx -s reload")
        logging.info("nginx reload success")

        time.sleep(10)

        status = current_platform_stats()
        if status == "mem_warning" or status == "cpu_warning":
            count += 1
            continue
        else:
            logging.info("DCP balance ok by add container")
            return


def takeReduceStrategy():
    logging.info("Reduce containers to solve...")
    count = 1
    flag = True

    while flag:
        container_list = []

        app_container_num = int(math.pow(2, count))
        logging.info("Reduce " + str(app_container_num) + " containers")

        pre_app_container_num = int(selectByKey(DCP_CONF_PATH, "app_container_num"))
        cur_app_container_num = pre_app_container_num - app_container_num

        if cur_app_container_num < 0:
            logging.error("container_num<0")
            return

        # create container
        for i in range(cur_app_container_num, pre_app_container_num):
            app_container_name = "APP-" + str(i)
            #  remove container
            remove_container(app_container_name)
            container_list.append(app_container_name)

            logging.info(app_container_name + " removed")

        insert(DCP_CONF_PATH, "app_container_num", str(cur_app_container_num))
        bulk_delete(DCP_DB_PATH, container_list)

        container_dict = selectAll(DCP_DB_PATH)

        config = get_nginx_config(container_dict)
        nginx_reload(config)
        os.system("/usr/local/nginx/sbin/nginx -s reload")

        time.sleep(10)

        status = current_platform_stats()
        if status == "mem_low_warning":
            count += 1
            continue
        else:
            logging.info("DCP balance ok by reduce container")
            return


def executeFalcon(interval):
    while True:
        falcon()
        time.sleep(interval / 1000)


if __name__ == '__main__':
    print ""
