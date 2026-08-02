# mechanism 8: a runtime transition to Done, in a reviewer path
def finish(item):
    item.status = "Done"
    return item
